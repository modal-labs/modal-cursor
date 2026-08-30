from __future__ import annotations

from unittest.mock import ANY, Mock

from logfire.testing import CaptureLogfire

from modal_cursor import Pool
from modal_cursor import controller as controller_module
from modal_cursor.controller import PoolSpec, _Dispatcher
from modal_cursor.registry import PendingRequest


def _request(pool: str = "gpu") -> PendingRequest:
    return PendingRequest(request_id="bc-1", pool=pool)


def test_dispatch_claims_then_provisions_matching_pool(monkeypatch) -> None:
    worker = Pool(name="gpu").machine(image="image")
    client = Mock()
    app = Mock()
    monkeypatch.setattr(controller_module, "claim_pending_request", Mock())
    monkeypatch.setattr(controller_module, "spawn_worker", Mock(return_value="sb-1"))
    dispatcher = _Dispatcher(app, (PoolSpec(Pool(name="gpu"), worker),), client)

    dispatcher._dispatch(_request(), claim_exists=False)
    dispatcher.close()

    controller_module.claim_pending_request.assert_called_once_with(client, "bc-1", ANY)
    controller_module.spawn_worker.assert_called_once()
    assert controller_module.spawn_worker.call_args.args[0].name == "gpu"
    assert controller_module.spawn_worker.call_args.args[3]["request_id"] == "bc-1"


def test_dispatch_releases_claim_when_provisioning_fails(monkeypatch) -> None:
    worker = Pool(name="gpu").machine(image="image")
    client = Mock()
    monkeypatch.setattr(controller_module, "claim_pending_request", Mock())
    monkeypatch.setattr(controller_module, "spawn_worker", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(controller_module, "release_claim", Mock())
    dispatcher = _Dispatcher(Mock(), (PoolSpec(Pool(name="gpu"), worker),), client)

    dispatcher._dispatch(_request(), claim_exists=False)
    dispatcher.close()

    controller_module.release_claim.assert_called_once_with(client, "bc-1")


def test_async_dispatch_is_visible_root_with_discovery_link(
    monkeypatch, capfire: CaptureLogfire
) -> None:
    worker = Pool(name="gpu").machine(image="image")
    client = Mock()
    monkeypatch.setattr(controller_module, "claim_pending_request", Mock())
    monkeypatch.setattr(controller_module, "spawn_worker", Mock(return_value="sb-1"))

    dispatcher = _Dispatcher(Mock(), (PoolSpec(Pool(name="gpu"), worker),), client)
    dispatcher.submit(_request(), claim_exists=False)
    dispatcher.close()

    spans = [
        item
        for item in capfire.exporter.exported_spans
        if item.attributes.get("logfire.span_type") == "span"
    ]
    dispatch = next(item for item in spans if item.name == "modal_cursor.controller.dispatch")
    assert dispatch.parent is None
    assert len(dispatch.links) == 1
    discovery = next(item for item in spans if item.name == "modal_cursor.controller.discover")
    assert discovery.parent is None
