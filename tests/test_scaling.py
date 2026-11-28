"""Autoscaler contract and read-only control-plane diagnostics."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from orchestration.scaling import inspect_scaling
from runtime.config.model_spec import ROOT, validate
from runtime.utils.files import read_json


def spec():
    return read_json(ROOT / "models/006-minimax-h3-image-vllm/model.json")


def fake_modal(*, runners=1, backlog=11, headroom=0, buffer=1):
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
    assert validate(spec())["buffer_containers"] == 1
    with pytest.raises(ValueError, match="buffer_containers"):
        validate({**spec(), "max_containers": 1})


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


def test_missing_buffer_detects_deployment_drift():
    modal, _ = fake_modal(buffer=None)
    report = inspect_scaling(spec(), modal_api=modal)
    assert not report["matches"]
    assert report["differences"] == {"buffer_containers": {"expected": 1, "actual": 0}}
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
