# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import grp
import json
import logging
import subprocess
import urllib.request
from pathlib import Path
from typing import List
from urllib.parse import urljoin

import requests
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)


async def get_unit_address(ops_test, app_name: str, unit_num: int) -> str:
    status = await ops_test.model.get_status()  # noqa: F821
    return status["applications"][app_name]["units"][f"{app_name}/{unit_num}"]["address"]


async def is_loki_up(ops_test, app_name, num_units=1) -> bool:
    # Sometimes get_unit_address returns a None, no clue why, so looping until it's not
    addresses = [""] * num_units
    while not all(addresses):
        addresses = [await get_unit_address(ops_test, app_name, i) for i in range(num_units)]

    def get(url) -> bool:
        try:
            response = urllib.request.urlopen(url, data=None, timeout=2.0)
            return response.code == 200 and "version" in json.loads(response.read())
        except Exception:
            return False

    count = 5
    while count >= 0:
        resp = [
            get(f"http://{address}:3100/loki/api/v1/status/buildinfo") for address in addresses
        ]
        if all(resp):
            return all(resp)
        # Otherwise, newer Loki may take a bit to re-play. Back off and wait a maximum of 5 seconds
        await asyncio.sleep(1)
        count -= 1
    return False


async def loki_rules(ops_test, app_name) -> dict:
    address = await get_unit_address(ops_test, app_name, 0)
    url = f"http://{address}:3100"

    try:
        response = urllib.request.urlopen(f"{url}/loki/api/v1/rules", data=None, timeout=2.0)
        if response.code == 200:
            return yaml.safe_load(response.read())
        return {}
    except urllib.error.HTTPError:
        return {}


async def loki_endpoint_request(ops_test, app_name: str, endpoint: str, unit_num: int = 0):
    address = await get_unit_address(ops_test, app_name, unit_num)
    url = urljoin(f"http://{address}:3100/", endpoint)
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return response.text
        return ""
    except requests.exceptions.RequestException:
        return ""


async def loki_api_query(ops_test, app_name, query: str, unit_num: int = 0):
    address = await get_unit_address(ops_test, app_name, unit_num)
    # Use query_range for a longer default time interval so we
    # don't need to nitpick about it
    url = f"http://{address}:3100/loki/api/v1/query_range"
    params = {"query": query}
    try:
        # Using requests because params with urllib are a mess
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json()["data"]["result"]
        return {}
    except requests.exceptions.RequestException:
        return {}


async def loki_alerts(ops_test: str, app_name: str, unit_num: int = 0, retries: int = 3) -> dict:
    r"""Get a list of alerts from a Prometheus-compatible endpoint.

    Results look like:
        {
          "data": {
              "groups": [
                  {
                      "rules": [
                          {
                              "alerts": [
                                  {
                                      "activeAt": "2018-07-04T20:27:12.60602144+02:00",
                                      "annotations": {
                                          "summary": "High request latency"
                                      },
                                      "labels": {
                                          "alertname": "HighRequestLatency",
                                          "severity": "page"
                                      },
                                      "state": "firing",
                                      "value": "1e+00"
                                  }
                              ],
                              "annotations": {
                                  "summary": "High request latency"
                              },
                              "duration": 600,
                              "health": "ok",
                              "labels": {
                                  "severity": "page"
                              },
                              "name": "HighRequestLatency",
                              "query": "job:request_latency_seconds:mean5m{job=\"myjob\"} > 0.5",
                              "type": "alerting"
                          },
                          {
                              "health": "ok",
                              "name": "job:http_inprogress_requests:sum",
                              "query": "sum by (job) (http_inprogress_requests)",
                              "type": "recording"
                          }
                      ],
                      "file": "/rules.yaml",
                      "interval": 60,
                      "limit": 0,
                      "name": "example"
                  }
              ]
          },
          "status": "success"
        }
    """
    address = await get_unit_address(ops_test, app_name, unit_num)
    url = f"http://{address}:3100/prometheus/api/v1/alerts"

    # Retry since the endpoint may not _immediately_ return valid data
    while not (
        alerts := json.loads(urllib.request.urlopen(url, data=None, timeout=2).read())["data"][
            "alerts"
        ]
    ):
        retries -= 1
        if retries > 0:
            await asyncio.sleep(2)
        else:
            break

    return alerts


async def get_alertmanager_alerts(ops_test: OpsTest, unit_name, unit_num, retries=3) -> List[dict]:
    """Get a list of alerts.

    Response looks like this:
    {
        'annotations': {'description': 'test-charm-...', 'summary': 'Instance test-charm-...'},
        'endsAt': '2021-09-03T21:03:59.658Z',
        'fingerprint': '4a0016cc12a07903',
        'receivers': [{'name': 'pagerduty'}],
        'startsAt': '2021-09-03T19:37:59.658Z',
        'status': {'inhibitedBy': [], 'silencedBy': [], 'state': 'active'},
        'updatedAt': '2021-09-03T20:59:59.660Z',
        'generatorURL': 'http://prometheus-0:9090/...',
        'labels': {
            'alertname': 'AlwaysFiring',
            'instance': 'test-charm-...',
            'job': 'juju_test-charm-...',
            'juju_application': 'tester', 'juju_model': 'test-charm-...',
            'juju_model_uuid': '...',
            'juju_unit': 'tester-0',
            'severity': 'Low',
            'status': 'testing'
        }
    }
    """
    address = await get_unit_address(ops_test, unit_name, unit_num)
    url = f"http://{address}:9093/api/v2/alerts"
    while not (alerts := json.loads(urllib.request.urlopen(url, data=None, timeout=2).read())):
        retries -= 1
        if retries > 0:
            await asyncio.sleep(2)
        else:
            break

    return alerts


class ModelConfigChange:
    """Context manager for temporarily changing a model config option."""

    def __init__(self, ops_test: OpsTest, config: dict):
        self.ops_test = ops_test
        self.change_to = config

    async def __aenter__(self):
        """On entry, the config is set to the user provided custom values."""
        config = await self.ops_test.model.get_config()
        self.revert_to = {k: config[k] for k in self.change_to.keys()}
        await self.ops_test.model.set_config(self.change_to)
        return self

    async def __aexit__(self, exc_type, exc_value, exc_traceback):
        """On exit, the modified config options are reverted to their original values."""
        await self.ops_test.model.set_config(self.revert_to)


def oci_image(metadata_file: str, image_name: str) -> str:
    """Find upstream source for a container image.

    Args:
        metadata_file: string path of metadata YAML file relative
            to top level charm directory
        image_name: OCI container image string name as defined in
            metadata.yaml file
    Returns:
        upstream image source
    Raises:
        FileNotFoundError: if metadata_file path is invalid
        ValueError: if upstream source for image name can not be found
    """
    metadata = yaml.safe_load(Path(metadata_file).read_text())

    resources = metadata.get("resources", {})
    if not resources:
        raise ValueError("No resources found")

    image = resources.get(image_name, {})
    if not image:
        raise ValueError("{} image not found".format(image_name))

    upstream_source = image.get("upstream-source", "")
    if not upstream_source:
        raise ValueError("Upstream source not found")

    return upstream_source


def uk8s_group() -> str:
    try:
        # Classically confined microk8s
        uk8s_group = grp.getgrnam("microk8s").gr_name
    except KeyError:
        # Strictly confined microk8s
        uk8s_group = "snap_microk8s"
    return uk8s_group


async def juju_show_unit(
    ops_test: OpsTest,
    unit_name: str,
    *,
    endpoint: str = None,
    related_unit: str = None,
    app_data_only: bool = False,
) -> dict:
    """Helper function for obtaining output of `juju show-unit`.

    Args:
        ops_test: pytest-operator fixture,
        unit_name: app name and unit num, e.g. "loki-tester/0".
        endpoint: limit output to relation data for this relation only, e.g. "logging-consumer".
        related_unit: limit output to relation data for this unit only, e.g. "loki/0".
        app_data_only: limit output to application relation data.

    See https://github.com/juju/python-libjuju/issues/642.
    """
    endpoint_arg = f"--endpoint {endpoint}" if endpoint else ""
    related_unit_arg = f"--related-unit {related_unit}" if related_unit else ""
    app_data_arg = "--app" if app_data_only else ""
    cmd = filter(
        None,
        f"juju show-unit {unit_name} {endpoint_arg} {related_unit_arg} {app_data_arg}".split(" "),
    )

    retcode, stdout, stderr = await ops_test.run(*cmd)
    assert retcode == 0, f"`juju show-unit` failed: {(stderr or stdout).strip()}"

    # Response looks like this:
    #
    # $ juju show-unit grafana-agent-k8s/0
    # grafana-agent-k8s/0:
    #   opened-ports: []
    #   charm: ch:amd64/focal/grafana-agent-k8s-7
    #   leader: true
    #   relation-info:
    #   - endpoint: logging-consumer
    #     related-endpoint: logging
    #     application-data:
    #       endpoints: '[{"url": "http://loki-k8s-0...local:3100/loki/api/v1/push"}]'
    #       promtail_binary_zip_url: https://.../promtail-linux-amd64.zip
    #     related-units:
    #       loki-k8s/0:
    #         in-scope: true
    #         data:
    #           egress-subnets: 10.152.183.143/32
    #           ingress-address: 10.152.183.143
    #           private-address: 10.152.183.143
    #   provider-id: grafana-agent-k8s-0
    #   address: 10.1.50.210

    # Return the dict without the top-level key (which is the unit itself)
    return yaml.safe_load(stdout)[unit_name]


def initial_workload_is_ready(ops_test, app_names) -> bool:
    """Checks that the initial workload (ie. x/0) is ready.

    Args:
        ops_test: pytest-operator plugin
        app_names: array of application names to check for

    Returns:
        whether the workloads are active or not
    """
    return all(
        ops_test.model.applications[name].units[0].workload_status == "active"
        for name in app_names
    )


async def generate_log_file(
    model_name: str, app_name: str, unit_num: int, container_name: str, filepath: str
) -> bytes:
    cmd = [
        "juju",
        "ssh",
        "--model",
        model_name,
        "--container",
        container_name,
        f"{app_name}/{unit_num}",
        "flog",
        "-t",
        "log",
        "-w",
        "-o",
        filepath,
    ]
    try:
        res = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logger.error(e.stdout.decode())
        raise e
    return res.stdout
