"""Controller construction contracts with temporary state and no provider I/O."""

from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

from studio_platform import application_services, dstack_controller
from studio_platform.repository import Repository
from studio_platform.settings import Settings


class ControllerServicesTests(unittest.TestCase):
    def run_controller(self, *, broken_chat=False):
        # Import the composition module before patching the controller's repository
        # factory, so an import inside that patch cannot retain a mocked alias.
        self.assertIs(application_services.Repository, Repository)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = Settings(Path(temporary.name), auth_mode="local-test")
        # The executor/transport are fakes; no SDK or PostgreSQL is launched.
        object.__setattr__(settings, "generation_queue", "dbos")
        repo = Repository(settings.database_url)
        repo.generation_transport = Mock()
        self.addCleanup(repo.close)
        runtime, service, telemetry = Mock(), Mock(), Mock()
        error_client = Mock(dsn=None)
        executor = Mock()
        executor.__enter__ = Mock(return_value=executor)
        captured, closed = {}, []

        def make_executor(*args, **kwargs):
            captured.update(kwargs)
            return executor

        original_close = repo.close

        def close_repo():
            closed.append("repo")
            original_close()

        executor.close.side_effect = lambda: closed.append("executor")
        runtime.close.side_effect = lambda: closed.append("runtime")
        service.capacity.client.close.side_effect = lambda: closed.append("capacity")
        with ExitStack() as stack:
            replacements = {
                "studio_platform.settings.Settings.from_environment": Mock(
                    return_value=settings
                ),
                "studio_platform.repository.Repository": Mock(return_value=repo),
                "studio_platform.controller_liveness.Progress": Mock(),
                "studio_platform.dstack_operator.from_environment": Mock(
                    return_value=service
                ),
                "studio_platform.dstack_factory.create_runtime": Mock(
                    return_value=(runtime, Path(temporary.name) / "work")
                ),
                "studio_platform.modal_controller.load_extension": Mock(
                    return_value=None
                ),
                "studio_platform.dbos_transport.DurableExecutor": make_executor,
                "studio_platform.dstack_controller.configured_telemetry": Mock(
                    return_value=telemetry
                ),
                "studio_platform.error_monitoring.create_client": Mock(
                    return_value=error_client
                ),
                "studio_platform.dstack_controller.DstackController": Mock(
                    return_value=Mock(tick=Mock(return_value={"original": True}))
                ),
                "studio_platform.dstack_controller.signal.signal": Mock(),
            }
            for target, value in replacements.items():
                stack.enter_context(patch(target, new=value))
            stack.enter_context(patch.object(repo, "close", side_effect=close_repo))
            stack.enter_context(redirect_stdout(io.StringIO()))
            if broken_chat:
                stack.enter_context(
                    patch(
                        "studio_platform.quick_chat.QuickChatService.__init__",
                        side_effect=RuntimeError("synthetic constructor failure"),
                    )
                )
                with self.assertRaisesRegex(RuntimeError, "synthetic constructor"):
                    dstack_controller.main(["--once"])
            else:
                self.assertEqual(dstack_controller.main(["--once"]), 0)
        return captured, closed, repo

    def test_controller_preserves_account_identity_and_original_close_order(self):
        captured, closed, repo = self.run_controller()
        sessions, deletion = captured["account_services"]
        self.assertIs(sessions.repo, repo)
        self.assertIs(deletion.repo, repo)
        self.assertIs(sessions.auth, deletion.auth)
        self.assertIs(sessions.workos, deletion.workos)
        self.assertIs(sessions.auth.session_checker.__self__, sessions)
        self.assertIs(deletion.chat.repo, repo)
        self.assertIs(deletion.chat.assets.store, deletion.chat.storage)
        self.assertEqual(closed, ["executor", "runtime", "capacity", "repo"])

    def test_controller_boot_needs_no_http_application_or_route_registration(self):
        blocked = {
            "studio_platform." + name: None
            for name in (
                "api",
                "frontend",
                "payments_routes",
                "quick_chat_routes",
                "story_chat_routes",
                "film_routes",
                "account_session_routes",
                "account_deletion_routes",
                "workos_routes",
            )
        }
        with patch.dict(sys.modules, blocked):
            captured, closed, _ = self.run_controller()
        self.assertIsNotNone(captured["film_setup"])
        self.assertEqual(closed, ["executor", "runtime", "capacity", "repo"])

    def test_constructor_failure_does_not_enter_executor_or_lose_owned_cleanup(self):
        captured, closed, _ = self.run_controller(broken_chat=True)
        self.assertEqual(captured, {})
        self.assertEqual(closed, ["runtime", "capacity", "repo"])

    def test_controller_film_setup_keeps_original_grant_revalidator(self):
        captured, _, repo = self.run_controller()
        turns = Mock()
        with patch("studio_platform.film_workflows.register_film_workflows") as setup:
            captured["film_setup"](Mock(), turns)
        _, film, principal_for = setup.call_args.args
        self.assertIs(setup.call_args.kwargs["domain_turns"], turns)
        sessions, _ = captured["account_services"]
        grant, principal = {"original": "grant"}, Mock()
        with (
            patch.object(film, "execution_context", return_value=grant) as context,
            patch.object(
                sessions.auth, "workflow_principal", return_value=principal
            ) as revalidate,
        ):
            self.assertIs(principal_for("tenant", "owner", "project", "run"), principal)
        context.assert_called_once_with("tenant", "owner", "project", "run")
        revalidate.assert_called_once_with("owner", grant)
        repo.generation_transport.client.register_queue.assert_called_once_with(
            "inkseq-film-v1", polling_interval_sec=10
        )


if __name__ == "__main__":
    unittest.main()
