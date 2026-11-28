"""Autoscaler contract and read-only control-plane diagnostics."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from orchestration.scaling import BatchCapacity, capacity_decision, inspect_scaling, restore_capacity
from tests.test_provisioning import Registry
from runtime.config.model_spec import ROOT, validate
from runtime.utils.files import read_json


def spec():
    return read_json(ROOT / "models/006-minimax-h3-image-vllm/model.json")


def fake_modal(*, runners=1, backlog=11, headroom=0, buffer=0):
    modal = Mock()
    app = NS(functions={"H3.*": "fu-model", "provision_info": "fu-probe"},
             lifecycle=NS(version=3))
    modal.App.lookup.return_value.info.return_value = app
    fn = modal.Function.from_id.return_value
    fn.autoscaler.settings.return_value = NS(min_containers=None,
        max_containers=2, buffer_containers=buffer, scaledown_window=60)
    fn.info.return_value = NS(concurrency_info=NS(max_inputs=2, target_inputs=1))
    fn.get_current_stats.return_value = NS(backlog=backlog, num_total_runners=runners,
        num_running_inputs=2 if runners else 0, input_headroom=headroom)
    return modal, fn


@pytest.mark.parametrize("value", [-1, True, "1", 1.5, 2, 3, None])
def test_invalid_buffer_containers(value):
    with pytest.raises(ValueError, match="buffer_containers"):
        validate({**spec(), "buffer_containers": value})


def test_buffer_zero_and_one_are_valid():
    assert validate({**spec(), "buffer_containers": 0})["buffer_containers"] == 0
    assert validate(spec())["buffer_containers"] == 0
    assert validate({**spec(), "buffer_containers": 1})["buffer_containers"] == 1
    with pytest.raises(ValueError, match="buffer_containers"):
        validate({**spec(), "max_containers": 1, "buffer_containers": 1})


def test_real_control_plane_single_runner_with_backlog_is_not_config_error():
    modal, fn = fake_modal()
    report = inspect_scaling(spec(), modal_api=modal)
    assert report["matches"]
    assert report["status"] == "backlog_below_container_limit"
    assert report["capacity"] == dict(backlog=11, runners=1, running_inputs=2,
                                      input_headroom=0)
    modal.Function.from_id.assert_called_once_with("fu-model")
    modal.Function.from_name.assert_not_called()
    modal.Cls.from_name.assert_not_called()
    fn.remote.assert_not_called()
    fn.spawn.assert_not_called()
    fn.autoscaler.update.assert_not_called()
    fn.update_autoscaler.assert_not_called()


def test_extra_buffer_detects_deployment_drift():
    modal, _ = fake_modal(buffer=1)
    report = inspect_scaling(spec(), modal_api=modal)
    assert not report["matches"]
    assert report["differences"] == {"buffer_containers": {"expected": 0, "actual": 1}}
    assert report["status"] == "configuration_mismatch"


def test_actual_concurrency_mismatch_is_not_hidden():
    modal, fn = fake_modal()
    fn.info.return_value.concurrency_info.target_inputs = 2
    report = inspect_scaling(spec(), modal_api=modal)
    assert not report["matches"]
    assert report["differences"]["target_inputs"] == {"expected": 1, "actual": 2}


@pytest.mark.parametrize("runners,backlog,headroom,status", [
    (2, 11, 0, "container_limit_reached"),
    (0, 0, 0, "no_backlog"),
    (1, 2, 1, "capacity_available_or_starting"),
])
def test_capacity_states(runners, backlog, headroom, status):
    modal, _ = fake_modal(runners=runners, backlog=backlog, headroom=headroom)
    report = inspect_scaling(spec(), modal_api=modal)
    assert report["matches"]
    assert report["status"] == status


def test_missing_class_service_does_not_probe_wrong_function():
    modal, _ = fake_modal()
    modal.App.lookup.return_value.info.return_value.functions = {"provision_info": "fu-probe"}
    with pytest.raises(RuntimeError, match="Deployed class function not found"):
        inspect_scaling(spec(), modal_api=modal)
    modal.Function.from_id.assert_not_called()


def test_rolling_deploy_race_rejected():
    modal, _ = fake_modal()
    app = modal.App.lookup.return_value.info.return_value
    modal.App.lookup.return_value.info.side_effect = [
        app, NS(functions=app.functions, lifecycle=NS(version=4))]
    with pytest.raises(RuntimeError, match="Deployment changed"):
        inspect_scaling(spec(), modal_api=modal)


def test_legacy_default_concurrency_and_buffer():
    modal, fn = fake_modal(runners=0, backlog=0, buffer=None)
    legacy = {**spec(), "max_containers": 1, "max_inputs": 1}
    legacy.pop("target_inputs")
    legacy.pop("buffer_containers")
    fn.info.return_value.concurrency_info = None
    fn.autoscaler.settings.return_value.max_containers = 1
    report = inspect_scaling(legacy, modal_api=modal)
    assert report["matches"]
    assert report["status"] == "no_backlog"


@pytest.mark.parametrize("count,expected", [(0, False), (8, False), (12, False),
                                           (19, False), (20, True), (24, True)])
def test_cold_second_runner_requires_enough_remaining_work(count, expected):
    decision = capacity_decision(count)
    assert decision["request"] is expected
    assert decision["two_runner_seconds"] <= decision["one_runner_seconds"]


def test_capacity_prediction_uses_whole_images_and_delay():
    decision = capacity_decision(24)
    assert decision["one_runner_seconds"] == 300
    assert decision["two_runner_seconds"] == 242.5
    assert decision["estimated_gain_seconds"] == 57.5
    assert not capacity_decision(24, startup_seconds=300)["request"]
    assert not capacity_decision(100, runners=2)["request"]
    assert not capacity_decision(100, max_containers=1)["request"]
    assert capacity_decision(8, startup_seconds=0)["request"]


@pytest.mark.parametrize("bad", [-1, True, 2.5])
def test_capacity_rejects_invalid_remaining_count(bad):
    with pytest.raises(ValueError):
        capacity_decision(bad)


def batch_state(tmp_path, count=24):
    from runtime.utils.files import atomic_json
    keys = tuple(f"image/{i:03}" for i in range(count))
    atomic_json(tmp_path / "state.json",
                {"plan_hash": "plan", "jobs": {key: {"status": "pending"} for key in keys}})
    return keys


def test_active_request_releases_after_parallel_assignment(tmp_path):
    keys = batch_state(tmp_path)
    modal, fn = fake_modal(runners=0, backlog=0)
    registry = Registry()
    settings = fn.autoscaler.settings.return_value
    stats = fn.get_current_stats.return_value

    def update(**changes):
        for key, value in changes.items():
            setattr(settings, key, value)
        if changes["min_containers"] == 2:
            assert registry["capacity-override:" + spec()["app_name"]]["before"]["min_containers"] == 0
            stats.num_total_runners = 2
            stats.num_running_inputs = 3

    fn.autoscaler.update.side_effect = update
    controller = BatchCapacity(modal, registry)
    controller.stop.wait = Mock(return_value=False)
    try:
        controller._run(tmp_path, "plan", keys, spec(), 3)
    finally:
        controller.close()
    assert [c.kwargs for c in fn.autoscaler.update.call_args_list] == [
        {"min_containers": 2, "buffer_containers": 0},
        {"min_containers": 0, "buffer_containers": 0},
    ]
    assert not registry
    fn.spawn.assert_not_called()


def test_lost_override_reply_is_restored_without_resubmission(tmp_path):
    keys = batch_state(tmp_path)
    modal, fn = fake_modal(runners=0, backlog=0)
    registry = Registry()
    settings = fn.autoscaler.settings.return_value

    def update(**changes):
        for key, value in changes.items():
            setattr(settings, key, value)
        if changes["min_containers"] == 2:
            raise OSError("reply lost after acceptance")

    fn.autoscaler.update.side_effect = update
    controller = BatchCapacity(modal, registry)
    try:
        controller._run(tmp_path, "plan", keys, spec(), 3)
    finally:
        controller.close()
    assert fn.autoscaler.update.call_count == 2
    assert settings.min_containers == 0
    assert not registry


@pytest.mark.parametrize("revision,count", [(4, 24), (3, 8)])
def test_capacity_does_not_mutate_wrong_revision_or_short_batch(tmp_path, revision, count):
    keys = batch_state(tmp_path, count=count)
    modal, fn = fake_modal(runners=0, backlog=0)
    registry = Registry()
    controller = BatchCapacity(modal, registry)
    try:
        controller._run(tmp_path, "plan", keys, spec(), revision)
    finally:
        controller.close()
    fn.autoscaler.update.assert_not_called()
    assert not registry


def test_abandoned_override_recovery_preserves_unrelated_setting_changes():
    modal, fn = fake_modal()
    report = inspect_scaling(spec(), modal_api=modal)
    key = "capacity-override:" + spec()["app_name"]
    registry = Registry({key: {
        "identity": {"app_revision": report["app_revision"], "function_id": report["function_id"]},
        "before": {"min_containers": 0, "buffer_containers": 0},
        "after": {"min_containers": 2, "buffer_containers": 0},
        "other_settings": {k: v for k, v in report["actual"].items()
                           if k not in {"min_containers", "buffer_containers"}},
    }})
    settings = fn.autoscaler.settings.return_value
    settings.min_containers = 2
    settings.scaledown_window = 90
    with pytest.raises(RuntimeError, match="Other autoscaler settings changed"):
        restore_capacity(spec(), modal, registry)
    assert key in registry
    fn.autoscaler.update.assert_not_called()


def test_controller_deduplicates_batch_and_ignores_other_models(tmp_path):
    modal, _ = fake_modal()
    controller = BatchCapacity(modal, Registry())
    plan = {"hash": "plan", "services": {"minimax-omni": {"spec": spec()}},
            "jobs": [{"key": f"image/{i:03}", "uses": "minimax-omni.t2i",
                      "params": {"size": "4096x2304", "num_inference_steps": 6}}
                     for i in range(24)]}
    ready = {"version": "source", "app_revision": 3}
    members = {str(i): [f"image/{i:03}"] for i in range(24)}
    with pytest.MonkeyPatch.context() as mp:
        submit = Mock()
        mp.setattr(controller.pool, "submit", submit)
        try:
            controller.consider(tmp_path, plan, "moss", ready, members)
            plan["jobs"][0]["params"]["size"] = "2048x1152"
            controller.consider(tmp_path, plan, "minimax-omni", ready, members)
            submit.assert_not_called()
            plan["jobs"][0]["params"]["size"] = "4096x2304"
            controller.consider(tmp_path, plan, "minimax-omni", ready, members)
            controller.consider(tmp_path, plan, "minimax-omni", ready, members)
            submit.assert_called_once()
        finally:
            controller.close()
