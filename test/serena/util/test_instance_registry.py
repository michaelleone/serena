"""Tests for the instance registry."""

import tempfile

import pytest

from serena.util.instance_registry import (
    InstanceRegistry,
    InstanceState,
    LifecycleEventType,
)


@pytest.fixture
def temp_registry():
    """Create a registry with a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield InstanceRegistry(base_dir=tmpdir)


class TestInstanceRegistry:
    def test_register_new_instance(self, temp_registry):
        """Test registering a new instance."""
        info = temp_registry.register(
            pid=1234,
            port=24282,
            context="test-context",
            modes=["mode1", "mode2"],
        )

        assert info.pid == 1234
        assert info.port == 24282
        assert info.context == "test-context"
        assert info.modes == ["mode1", "mode2"]
        assert info.state == InstanceState.LIVE_NO_PROJECT.value

    def test_update_project(self, temp_registry):
        """Test updating project for an instance."""
        temp_registry.register(pid=1234, port=24282)

        temp_registry.update_project(
            pid=1234,
            project_name="test-project",
            project_root="/path/to/project",
        )

        inst = temp_registry.get_instance(1234)
        assert inst is not None
        assert inst.project_name == "test-project"
        assert inst.project_root == "/path/to/project"
        assert inst.state == InstanceState.LIVE_WITH_PROJECT.value

    def test_mark_zombie(self, temp_registry):
        """Test marking an instance as zombie."""
        temp_registry.register(pid=1234, port=24282)

        temp_registry.mark_zombie(1234)

        inst = temp_registry.get_instance(1234)
        assert inst is not None
        assert inst.state == InstanceState.ZOMBIE.value
        assert inst.zombie_detected_at is not None

    def test_prune_zombies(self, temp_registry):
        """Test pruning old zombies."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.mark_zombie(1234)

        # Should not prune immediately
        pruned = temp_registry.prune_zombies(timeout_seconds=300)
        assert pruned == []

        # Should prune with 0 timeout
        pruned = temp_registry.prune_zombies(timeout_seconds=0)
        assert pruned == [1234]

        # Should be gone
        assert temp_registry.get_instance(1234) is None

    def test_lifecycle_events(self, temp_registry):
        """Test that lifecycle events are recorded."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.update_project(1234, "test-project")
        temp_registry.mark_zombie(1234)

        events = temp_registry.get_lifecycle_events()

        event_types = [e.event_type for e in events]
        assert LifecycleEventType.INSTANCE_STARTED.value in event_types
        assert LifecycleEventType.PROJECT_ACTIVATED.value in event_types
        assert LifecycleEventType.ZOMBIE_DETECTED.value in event_types

    def test_unregister(self, temp_registry):
        """Test clean unregistration."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.unregister(1234)

        assert temp_registry.get_instance(1234) is None

        events = temp_registry.get_lifecycle_events()
        event_types = [e.event_type for e in events]
        assert LifecycleEventType.INSTANCE_STOPPED.value in event_types

    def test_list_instances(self, temp_registry):
        """Test listing all instances."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.register(pid=5678, port=24283)

        instances = temp_registry.list_instances()
        assert len(instances) == 2
        pids = {inst.pid for inst in instances}
        assert pids == {1234, 5678}

    def test_update_heartbeat(self, temp_registry):
        """Test heartbeat update."""
        temp_registry.register(pid=1234, port=24282)
        original_time = temp_registry.get_instance(1234).last_heartbeat

        # Update heartbeat
        temp_registry.update_heartbeat(1234)

        inst = temp_registry.get_instance(1234)
        assert inst.last_heartbeat >= original_time

    def test_zombie_restoration(self, temp_registry):
        """Test that zombie is restored on heartbeat."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.mark_zombie(1234)

        assert temp_registry.get_instance(1234).state == InstanceState.ZOMBIE.value

        # Heartbeat should restore
        temp_registry.update_heartbeat(1234)
        inst = temp_registry.get_instance(1234)
        assert inst.state == InstanceState.LIVE_NO_PROJECT.value
        assert inst.zombie_detected_at is None

        # Check restoration event
        events = temp_registry.get_lifecycle_events()
        event_types = [e.event_type for e in events]
        assert LifecycleEventType.HEARTBEAT_RESTORED.value in event_types

    def test_global_dashboard_tracking(self, temp_registry):
        """Test global dashboard PID/port tracking."""
        temp_registry.set_global_dashboard(pid=1234, port=25282)

        assert temp_registry.get_global_dashboard_port() == 25282

        # Clear with wrong PID should not clear
        temp_registry.clear_global_dashboard(pid=9999)
        assert temp_registry.get_global_dashboard_port() == 25282

        # Clear with correct PID should clear
        temp_registry.clear_global_dashboard(pid=1234)
        assert temp_registry.get_global_dashboard_port() is None

    def test_record_force_kill(self, temp_registry):
        """Test recording force kill."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.mark_zombie(1234)

        temp_registry.record_force_kill(1234, success=True)

        # Instance should be removed on successful kill
        assert temp_registry.get_instance(1234) is None

        # Event should be recorded
        events = temp_registry.get_lifecycle_events()
        event_types = [e.event_type for e in events]
        assert LifecycleEventType.ZOMBIE_FORCE_KILLED.value in event_types

    def test_project_deactivation(self, temp_registry):
        """Test project deactivation event."""
        temp_registry.register(pid=1234, port=24282)
        temp_registry.update_project(1234, "test-project")
        temp_registry.update_project(1234, None)

        inst = temp_registry.get_instance(1234)
        assert inst.project_name is None
        assert inst.state == InstanceState.LIVE_NO_PROJECT.value

        events = temp_registry.get_lifecycle_events()
        event_types = [e.event_type for e in events]
        assert LifecycleEventType.PROJECT_DEACTIVATED.value in event_types
