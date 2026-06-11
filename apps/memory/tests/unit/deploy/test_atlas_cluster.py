"""Unit tests for the Atlas cluster IaC request-body builders.

Only the pure ``ClusterSpec`` request-shaping logic is unit-tested here — the
``AtlasClient`` HTTP layer is an external boundary (live Atlas Admin API) and is
exercised manually via ``make memory-atlas-*``, not mocked here. The script is
loaded via ``importlib.util.spec_from_file_location`` so ``deploy/`` need not be
on ``sys.path``.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "atlas_cluster.py"
_spec = importlib.util.spec_from_file_location("atlas_cluster", _SCRIPT)
_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec is not None and _spec.loader is not None
# Register before exec: ``ClusterSpec`` is a dataclass, and dataclass field
# resolution under ``from __future__ import annotations`` looks the module up in
# ``sys.modules`` by name — absent that entry it raises at class-definition time.
sys.modules["atlas_cluster"] = _module
_spec.loader.exec_module(_module)

ClusterSpec = _module.ClusterSpec


def test_free_tier_region_config_is_tenant_backed() -> None:
    # Arrange
    spec = ClusterSpec(tier="M0", provider="GCP", region="WESTERN_EUROPE")

    # Act
    config = spec.region_config()

    # Assert — M0 is a TENANT cluster naming the backing cloud; no nodeCount.
    assert config["providerName"] == "TENANT"
    assert config["backingProviderName"] == "GCP"
    assert config["regionName"] == "WESTERN_EUROPE"
    assert config["electableSpecs"] == {"instanceSize": "M0"}


def test_dedicated_tier_region_config_names_provider_directly() -> None:
    # Arrange
    spec = ClusterSpec(tier="M10", provider="AWS", region="US_EAST_1")

    # Act
    config = spec.region_config()

    # Assert — dedicated tiers use providerName directly + a 3-node electable set.
    assert config["providerName"] == "AWS"
    assert "backingProviderName" not in config
    assert config["electableSpecs"] == {"instanceSize": "M10", "nodeCount": 3}


def test_is_free_tier_flag() -> None:
    assert ClusterSpec(tier="M0").is_free_tier is True
    assert ClusterSpec(tier="M10").is_free_tier is False


def test_create_body_is_a_replicaset_with_one_region_config() -> None:
    # Arrange
    spec = ClusterSpec(cluster_name="tree", tier="M0")

    # Act
    body = spec.create_body()

    # Assert
    assert body["name"] == "tree"
    assert body["clusterType"] == "REPLICASET"
    region_configs = body["replicationSpecs"][0]["regionConfigs"]
    assert len(region_configs) == 1
    assert region_configs[0]["providerName"] == "TENANT"


def test_db_user_body_grants_read_write_on_admin() -> None:
    # Arrange
    spec = ClusterSpec(db_username="tree_user", db_password="secret")

    # Act
    body = spec.db_user_body()

    # Assert
    assert body["username"] == "tree_user"
    assert body["password"] == "secret"
    assert body["databaseName"] == "admin"
    assert body["roles"] == [
        {"roleName": "readWriteAnyDatabase", "databaseName": "admin"}
    ]
