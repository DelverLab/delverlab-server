package com.delverlab.server;

import java.sql.SQLException;
import javax.sql.DataSource;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@SpringBootApplication
public class ServerApplication {

	public static void main(String[] args) {
		SpringApplication.run(ServerApplication.class, args);
	}

}

@RestController
class ReadinessController {

	private final DataSource dataSource;

	ReadinessController(DataSource dataSource) {
		this.dataSource = dataSource;
	}

	@GetMapping("/healthz")
	ResponseEntity<Void> readiness() {
		try (var connection = dataSource.getConnection()) {
			if (connection.isValid(2)) {
				return ResponseEntity.noContent().build();
			}
		} catch (SQLException ignored) {
			// Do not expose database details in a health response.
		}
		return ResponseEntity.status(503).build();
	}
}
