"""
Tests for the session management module.
"""

import threading
import time

import pytest

from serena.session import Session, SessionInfo, SessionManager, SessionState


class TestSession:
    """Tests for the Session class."""

    def test_session_creation(self) -> None:
        """Test basic session creation."""
        session = Session(client_name="test-client")
        assert session.session_id is not None
        assert session.client_name == "test-client"
        assert session.state == SessionState.CONNECTED
        assert session.get_active_project() is None
        assert len(session.get_active_modes()) == 0

    def test_session_id_generation(self) -> None:
        """Test that session IDs are unique UUIDs."""
        session1 = Session()
        session2 = Session()
        assert session1.session_id != session2.session_id
        assert len(session1.session_id) == 36  # UUID format

    def test_session_custom_id(self) -> None:
        """Test that custom session IDs are accepted."""
        custom_id = "custom-session-id"
        session = Session(session_id=custom_id)
        assert session.session_id == custom_id

    def test_session_touch(self) -> None:
        """Test that touch() updates last_activity."""
        session = Session()
        initial_activity = session.last_activity
        time.sleep(0.01)
        session.touch()
        assert session.last_activity > initial_activity

    def test_session_state_changes(self) -> None:
        """Test session state transitions."""
        session = Session()
        assert session.state == SessionState.CONNECTED

        session.state = SessionState.ACTIVE
        assert session.state == SessionState.ACTIVE

        session.disconnect()
        assert session.state == SessionState.DISCONNECTED

    def test_session_tool_calls(self) -> None:
        """Test tool call tracking."""
        session = Session()
        assert session.get_tool_call_count() == 0

        session.increment_tool_calls("find_symbol")
        assert session.get_tool_call_count() == 1

        session.increment_tool_calls("find_symbol")
        session.increment_tool_calls("search_for_pattern")
        assert session.get_tool_call_count() == 3

        stats = session.get_tool_stats()
        assert stats["find_symbol"] == 2
        assert stats["search_for_pattern"] == 1

    def test_session_info(self) -> None:
        """Test SessionInfo serialization."""
        session = Session(client_name="test-client")
        info = session.get_info()

        assert isinstance(info, SessionInfo)
        assert info.session_id == session.session_id
        assert info.client_name == "test-client"
        assert info.state == SessionState.CONNECTED

        # Test to_dict
        info_dict = info.to_dict()
        assert info_dict["session_id"] == session.session_id
        assert info_dict["client_name"] == "test-client"
        assert info_dict["state"] == "connected"

    def test_session_thread_safety(self) -> None:
        """Test that session operations are thread-safe."""
        session = Session()
        results: list[int] = []

        def increment_many() -> None:
            for _ in range(100):
                session.increment_tool_calls("test")
                results.append(session.get_tool_call_count())

        threads = [threading.Thread(target=increment_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert session.get_tool_call_count() == 500


class TestSessionManager:
    """Tests for the SessionManager class."""

    def test_session_manager_creation(self) -> None:
        """Test basic session manager creation."""
        manager = SessionManager()
        assert manager.get_session_count() == 0
        assert manager.get_active_session_count() == 0

    def test_create_session(self) -> None:
        """Test session creation through manager."""
        manager = SessionManager()
        session = manager.create_session(client_name="test")

        assert session is not None
        assert manager.get_session_count() == 1
        assert manager.get_session(session.session_id) is session

    def test_list_sessions(self) -> None:
        """Test listing all sessions."""
        manager = SessionManager()
        session1 = manager.create_session(client_name="client1")
        session2 = manager.create_session(client_name="client2")

        sessions = manager.list_sessions()
        assert len(sessions) == 2
        assert session1 in sessions
        assert session2 in sessions

    def test_list_session_infos(self) -> None:
        """Test listing session infos."""
        manager = SessionManager()
        manager.create_session(client_name="client1")
        manager.create_session(client_name="client2")

        infos = manager.list_session_infos()
        assert len(infos) == 2
        assert all(isinstance(info, SessionInfo) for info in infos)

    def test_remove_session(self) -> None:
        """Test session removal."""
        manager = SessionManager()
        session = manager.create_session(client_name="test")
        session_id = session.session_id

        assert manager.get_session_count() == 1
        result = manager.remove_session(session_id)
        assert result is True
        assert manager.get_session_count() == 0
        assert manager.get_session(session_id) is None

    def test_remove_nonexistent_session(self) -> None:
        """Test removing a session that doesn't exist."""
        manager = SessionManager()
        result = manager.remove_session("nonexistent-id")
        assert result is False

    def test_disconnect_session(self) -> None:
        """Test disconnecting a session."""
        manager = SessionManager()
        session = manager.create_session(client_name="test")
        session_id = session.session_id

        result = manager.disconnect_session(session_id)
        assert result is True
        assert session.state == SessionState.DISCONNECTED
        # Session still exists after disconnect
        assert manager.get_session(session_id) is session

    def test_get_active_sessions(self) -> None:
        """Test filtering active sessions."""
        manager = SessionManager()
        session1 = manager.create_session(client_name="client1")
        session2 = manager.create_session(client_name="client2")
        session3 = manager.create_session(client_name="client3")

        manager.disconnect_session(session2.session_id)

        active = manager.get_active_sessions()
        assert len(active) == 2
        assert session1 in active
        assert session3 in active
        assert session2 not in active

    def test_session_manager_shutdown(self) -> None:
        """Test session manager shutdown."""
        manager = SessionManager()
        session1 = manager.create_session(client_name="client1")
        session2 = manager.create_session(client_name="client2")

        manager.shutdown()

        assert session1.state == SessionState.DISCONNECTED
        assert session2.state == SessionState.DISCONNECTED
