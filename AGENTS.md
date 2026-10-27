# AGENTS.md — DelverLab Server

Implementation rules for anyone (human or agent) working on `delverlab-server`.

- **What we build** is described in [`docs/architecture.md`](docs/architecture.md). It is the source of truth for concepts, the data model and invariants.
- **How we build it** is described here.

The architecture is a draft. If the code needs to diverge from it, propose a change to the doc first. Do not diverge silently.

---

## 1. Stack

The stack is fixed. Do not change versions or add dependencies without approval.

| Concern | Choice |
|---|---|
| Language | Java 27 (Gradle toolchain) |
| Framework | Spring Boot 4.1 (Web MVC, Data JPA) |
| Build | Gradle 9 with the Kotlin DSL (`build.gradle.kts`), always through the wrapper |
| Database | PostgreSQL 15+ (uses enums, `jsonb`, `gen_random_uuid()`, `NULLS NOT DISTINCT`, triggers) |
| Migrations | Flyway |
| Data access | JPA for simple entities; `JdbcTemplate` for `jsonb`, recursive CTEs, generated columns and bulk ingestion |
| Auth | Spring Security with JWT for people; device keys / tokens for headsets |
| File storage | `BlobStorage` interface: local filesystem (default) or S3-compatible (Garage) |
| Live status | SSE |
| API contract | OpenAPI, used to generate the TypeScript (web) and C# (Unity) clients |

Base package: `com.delverlab.server`

---

## 2. Local development

Spring Boot runs on the host. Its dependencies run in Docker through `compose.dev.yaml`.

| Service | Purpose | Address |
|---|---|---|
| `postgres` | Main database | `localhost:5432`, database `delverlab` |
| `garage` | S3-compatible object storage | S3 API `http://localhost:3900`, admin API `http://localhost:3903` |

Dev credentials (database user, S3 key, bucket) are in `application-dev.yaml` and match the compose file. They are for local use only.

```bash
# start / stop dependencies
docker compose -f compose.dev.yaml up -d
docker compose -f compose.dev.yaml down

# wipe all local data (database + objects)
docker compose -f compose.dev.yaml down -v

# run the server with the dev profile
./gradlew bootRun --args='--spring.profiles.active=dev'

# test / full build
./gradlew test
./gradlew build
```

On Windows PowerShell, use `.\gradlew.bat` in place of `./gradlew`.

Docker-related config files (Garage config, init scripts) live in `docker/`.

---

## 3. Project structure

### 3.1 Package by resource, then by layer

Code is grouped by **resource** first. Inside each resource, code is split by **layer**.

A resource is one concept of the system (template, experiment, session, ...). It usually matches a REST resource and owns its tables.

```
src/main/java/com/delverlab/server/
├── ServerApplication.java
├── common/                  # cross-cutting: config, error handling, shared utilities
├── template/
│   ├── controller/          # TemplateController
│   ├── service/             # TemplateService
│   ├── repository/          # TemplateRepository
│   ├── entity/              # TemplateEntity
│   ├── model/               # enums, value objects
│   ├── dto/                 # CreateTemplateRequest, TemplateResponse
│   └── mapper/              # TemplateMapper
├── design/
│   └── ...                  # same layout
└── ...

src/main/resources/
├── application.yaml
├── application-dev.yaml
└── db/migration/            # Flyway: V1__init.sql, V2__..., ...
```

Only create the layer packages a resource needs. For example, `plan` has no `entity` or `repository`.

### 3.2 Layers

| Package | Contains | Rules |
|---|---|---|
| `controller` | `@RestController` classes | HTTP only: routing, request validation, status codes. No business logic. |
| `service` | `@Service` classes | Business logic, validation, transactions. The only layer other resources may call. |
| `repository` | Spring Data JPA interfaces and `JdbcTemplate` classes | Data access for this resource's tables only. |
| `entity` | JPA `@Entity` classes | One class per table owned by this resource. |
| `model` | Enums, value objects, internal records | Types used inside the resource that are not tables and not API contract. |
| `dto` | Request and response records | The API contract. |
| `mapper` | Entity ↔ DTO / model mapping | Plain, hand-written classes. |

### 3.3 Resources (initial map)

| Package | Owns tables | Responsibility |
|---|---|---|
| `template` | `templates` | Task kinds |
| `variable` | `variables` | Variable identity; immutable fields |
| `schema` | `schema_versions`, `schema_variables` | Schema versions, publishing, hashing |
| `build` | `builds` | Build registration from the Unity editor |
| `experiment` | `experiments` | Experiments and the active design version |
| `design` | `design_versions`, `design_levels`, `conditions`, `design_nodes`, `design_values`, `design_factors` | Design editing, locking, value cascade |
| `participant` | `participants` | Participant enrolment |
| `device` | `devices` | Device registration and device auth |
| `session` | `sessions`, `session_nodes`, `recorded_values`, `session_events` | Session creation, result ingestion |
| `file` | `files` | File metadata and `BlobStorage` (local / S3) |
| `plan` | — | Plan engine: design + seed + condition → session plan |
| `export` | — | Data exports |
| `common` | — | Shared infrastructure. Never depends on a resource. |

Each table is owned by exactly one resource. Adding, renaming or merging resources needs approval.

---

## 4. Dependency rules

These rules are strict.

```
          ┌────────────── resource A ──────────────┐   ┌────── resource B ──────┐
 HTTP ──► │ controller ──► service ──► repository  │   │ service ──► repository │
          └───────────────────┬────────────────────┘   └────▲───────────────────┘
                              └─────────────────────────────┘
          A's service may call B's service, never B's repository or entities
```

1. **Controllers** call only their own resource's services. Never a repository.
2. **Services** may call their own resource's repositories and **other resources' services**.
3. **A service never uses another resource's repository or entity.**
4. **Entities never leave their resource.** Controllers return DTOs. Cross-resource service calls exchange DTOs, models or IDs.
5. **Cross-resource references in entities are plain IDs** (`UUID templateId`), not JPA associations. JPA associations (`@ManyToOne`, `@OneToMany`) are only used between entities of the same resource. Foreign keys still exist in the database.
6. **No circular dependencies between services.** If two resources need each other, move the logic to the resource that owns the data, or to an orchestrating service (for example `plan` reads from `design` and `schema`).
7. Any resource may use `common`. `common` never imports from a resource.

---

## 5. Layer conventions

### Controllers
- Base path: `/api/v1/<plural-resource>` (e.g. `/api/v1/templates`).
- Validate request bodies with `@Valid` and Jakarta Bean Validation annotations on the DTO.
- Return DTOs, never entities. Use `ResponseEntity` when the status is not 200.
- No `@Transactional` on controllers.

### Services
- Own the transaction boundaries: `@Transactional` on write methods, `@Transactional(readOnly = true)` on reads.
- Enforce business rules and invariants before hitting the database (see §7).
- Signal failures with the resource's own exceptions. They are translated to HTTP responses in `common` (see §8).

### Repositories
- Use **Spring Data JPA** interfaces for simple CRUD.
- Use a **`JdbcTemplate`** class (named `<Name>JdbcRepository`) for:
  - `jsonb` queries,
  - recursive CTEs (value cascade, tree walks),
  - tables with generated columns (`recorded_values`),
  - idempotent device ingestion (`insert ... on conflict do nothing`).
- Both kinds live in the resource's `repository` package and follow the same access rules.

### Entities
- Named `<Name>Entity` (e.g. `SessionEntity`). This avoids clashes with model/DTO types and with `org.hibernate.Session` / `java.io.File`.
- Map exactly to a table created by Flyway. Hibernate never creates the schema.
- IDs are `UUID`.
- Map `jsonb` with `@JdbcTypeCode(SqlTypes.JSON)`.
- Use `Instant` for `timestamptz`.
- Never use `@Data`, `@EqualsAndHashCode` or `@ToString` on entities. They trigger lazy loading, recurse through associations and break Hibernate identity.

### DTOs
- Java `record`s.
- Naming: `Create<Name>Request`, `Update<Name>Request`, `<Name>Response`.
- DTOs are the public API contract (§8). Changing an existing field is a breaking change.

### Mappers
- Named `<Name>Mapper`.
- Hand-written. No mapping library.

---

## 6. Database & migrations

- **Flyway is the only way to change the schema.** Set `spring.jpa.hibernate.ddl-auto: validate`.
- Migrations live in `src/main/resources/db/migration/` and are named `V<n>__<snake_case_description>.sql`.
- **Never edit a migration that is already on `main`.** Add a new one.
- SQL style follows `docs/architecture.md`: lowercase keywords, `snake_case`, plural table names.
- **Server-generated IDs** use `gen_random_uuid()` or a UUID created in Java.
- **Device-generated IDs** (`sessions`, `session_nodes`, `session_events`, `files`) are accepted as sent. Uploads are idempotent: `on conflict do nothing`.
- Every leaf stores its fully **resolved** values, including inherited ones.
- Tests use real PostgreSQL. Never H2, because the schema depends on Postgres-only features.

---

## 7. Invariants

These must never be broken. Database triggers are a safety net. Services check first and return a clear error (`409 Conflict`).

| Invariant | Enforced by |
|---|---|
| Variable identity (`key`, `kind`, `data_type`, `unit`) is never edited. Create a new variable instead. | `variable` service + trigger |
| Published schema versions are immutable. | `schema` service + trigger |
| Locked design versions are immutable. | `design` service + trigger |
| Recorded values are never rewritten. | `session` service |
| A design runs only on a build with the same schema version (`design.schema_version_id == build.schema_version_id`). | `session` / `plan` service |
| Every value actually used is recorded with its `source`. | `session` service |
| Values are validated against `data_type` and `constraints` in services, not in triggers. | owning service |

### Plan generation must be deterministic

- The plan engine is pure: no database access, no I/O, no clock. Inputs come in, a plan goes out.
- All randomness comes from the documented, seeded PRNG.
- Never use `Math.random()`, `java.util.Random`, `ThreadLocalRandom` or `Collections.shuffle` without the seeded PRNG.
- The engine must pass the shared test vectors (`design + seed + condition → expected plan`). The C# implementation in the Unity SDK must produce identical plans.

---

## 8. API

- REST + JSON under `/api/v1`. SSE for live status.
- Errors use RFC 9457 `ProblemDetail`, produced by one global `@RestControllerAdvice` in `common`.
- IDs are UUID strings. Timestamps are ISO-8601 in UTC.
- The OpenAPI spec generates the web and Unity clients. Breaking changes to DTOs or endpoints need approval.

---

## 9. Testing

- JUnit 5 through `./gradlew test`.
- **Unit tests** for services and the plan engine. Mock other resources' services, not repositories.
- **Integration tests** for repositories, migrations, triggers and controllers run against real PostgreSQL (Testcontainers).
- The plan engine is tested against the shared test vectors.
- `./gradlew build` must pass before merging.

---

## 10. Code style

- Indent Java with tabs, as in the generated sources.
- Constructor injection with `private final` fields and `@RequiredArgsConstructor`. No `@Autowired` on fields.
- Configuration through `@ConfigurationProperties` records under the `delverlab.*` prefix. Avoid scattered `@Value`.
- Log with SLF4J through Lombok's `@Slf4j`. No `System.out`.
- Never commit secrets. Only dev-only credentials may be in the repo, in `application-dev.yaml` and `compose.dev.yaml`.

### Lombok

Lombok removes boilerplate. It does not replace records.

| Use | For |
|---|---|
| `@RequiredArgsConstructor` | Controllers, services, components (constructor injection) |
| `@Slf4j` | Logging |
| `@Getter`, `@Setter`, `@NoArgsConstructor(access = PROTECTED)` | Entities (see §5) |
| `@Builder` | Classes with many optional fields, when it improves readability |

- DTOs, `@ConfigurationProperties` and value objects stay Java `record`s.
- Do not use `@Data`, `@Value` (use a record) or `@SneakyThrows`.

---

## 11. Git workflow

- `main` is always buildable. **Never commit directly to `main`.**
- Every feature or fix gets its own branch from an up-to-date `main`:

  | Prefix | Use |
  |---|---|
  | `feature/<kebab-name>` | New functionality |
  | `fix/<kebab-name>` | Bug fixes |
  | `chore/<kebab-name>` | Build, tooling, dependencies |
  | `docs/<kebab-name>` | Documentation only |

- Merge into `main` when the work is complete and `./gradlew build` passes. Delete the branch after merging.
- Commit messages: imperative subject of 72 characters or less (`Add schema version publishing`). Use the body to explain *why*.
- Commits use the developer's own git identity. Agents never add themselves as author or co-author, and never mention themselves in commit messages.

---

## 12. Working agreement for agents

Ask before you:

- add or upgrade a dependency, or change the stack;
- add, rename or merge a resource package;
- change an invariant (§7) or the dependency rules (§4);
- edit a migration that is already on `main`;
- make a breaking change to the public API;
- diverge from `docs/architecture.md`.

Keep changes scoped to the current feature branch.
