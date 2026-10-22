"""Neo4j transport shared by anchor resolution and graph traversal."""
from __future__ import annotations

import os
from typing import Any

from neo4j import GraphDatabase, Query, RoutingControl
from neo4j.exceptions import Neo4jError, ServiceUnavailable, DriverError


class GraphClientError(RuntimeError):
    """A safe, user-facing graph connection/query error."""


class Neo4jGraphClient:
    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j") -> None:
        try:
            self.driver = GraphDatabase.driver(
                uri, auth=(username, password), connection_timeout=20
            )
        except (DriverError, ValueError) as exc:
            raise GraphClientError(f"Could not create Neo4j driver: {type(exc).__name__}.") from exc
        self.database = database

    def query(self, cypher: str, parameters: dict | None = None) -> list[dict[str, Any]]:
        try:
            records, _, _ = self.driver.execute_query(
                Query(cypher, timeout=90), parameters_=parameters or {},
                database_=self.database, routing_=RoutingControl.READ,
            )
            return [record.data() for record in records]
        except (Neo4jError, ServiceUnavailable, DriverError) as exc:
            raise GraphClientError(f"Neo4j query failed: {type(exc).__name__}.") from exc

    def get_engine_status(self) -> dict[str, str]:
        try:
            self.driver.verify_connectivity()
            rows = self.query("CALL dbms.components() YIELD name, versions RETURN name, versions")
            version = ", ".join(rows[0].get("versions", [])) if rows else ""
            return {"status": "healthy", "db_engine_version": version, "role": "neo4j"}
        except GraphClientError:
            raise
        except (Neo4jError, ServiceUnavailable, DriverError) as exc:
            raise GraphClientError(f"Neo4j connectivity check failed: {type(exc).__name__}.") from exc

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "Neo4jGraphClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def create_graph_client(graph_config: dict) -> Neo4jGraphClient:
    provider = graph_config.get("provider", "neo4j")
    if provider != "neo4j":
        raise GraphClientError("graph.provider must be 'neo4j'.")
    uri = str(graph_config.get("uri", "")).strip()
    if not uri.startswith(("bolt://", "neo4j://", "bolt+s://", "neo4j+s://")):
        raise GraphClientError("Set graph.uri to a valid Bolt or Neo4j URI.")
    password_env = graph_config.get("password_env", "NEO4J_PASSWORD")
    password = os.environ.get(password_env)
    if not password:
        raise GraphClientError("Set the NEO4J_PASSWORD environment variable.")
    return Neo4jGraphClient(uri, graph_config.get("username", "neo4j"), password,
                            graph_config.get("database", "neo4j"))
