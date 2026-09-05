from __future__ import annotations

import unittest
from pathlib import Path


class DeploymentContractTests(unittest.TestCase):
    def test_read_only_image_disables_gunicorn_control_socket(self) -> None:
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("--no-control-socket", dockerfile)

    def test_compose_treats_password_derivatives_as_raw_environment_values(self) -> None:
        compose = (Path(__file__).parents[1] / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("format: raw", compose)

    def test_ci_exercises_compose_healthcheck(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("docker compose", workflow)
        self.assertIn("/healthz", workflow)


if __name__ == "__main__":
    unittest.main()