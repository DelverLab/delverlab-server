package com.delverlab.server;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.ActiveProfiles;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("dev")
class ServerApplicationTests {

	@LocalServerPort
	int port;

	@Test
	void reportsLivenessOverHttp() throws Exception {
		assertUp("/actuator/health/liveness");
	}

	@Test
	void reportsReadinessOverHttp() throws Exception {
		assertUp("/actuator/health/readiness");
		assertUp("/healthz");
	}

	private void assertUp(String path) throws Exception {
		try (var client = HttpClient.newHttpClient()) {
			var request = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path)).build();
			var response = client.send(request, HttpResponse.BodyHandlers.ofString());
			assertEquals(200, response.statusCode(), path);
			assertTrue(response.body().matches("\\{\\s*\"status\"\\s*:\\s*\"UP\"\\s*}"), response.body());
		}
	}

}
