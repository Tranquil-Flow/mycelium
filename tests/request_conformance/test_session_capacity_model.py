from mycelium_request_conformance.capacity import CapacityModel, SessionRecord


def test_capacity_rejects_when_every_session_is_live_without_side_effects():
    model = CapacityModel(max_sessions=2)
    state = model.initial_state()
    state = model.admit(state, "request-a").state
    state = model.admit(state, "request-b").state

    rejected = model.admit(state, "request-c")

    assert rejected.code == "gateway_capacity_exhausted"
    assert rejected.state.sessions == state.sessions
    assert rejected.state.admission_rejections == state.admission_rejections + 1
    assert rejected.state.runtime_starts == state.runtime_starts


def test_capacity_evicts_oldest_cleanup_complete_detached_session():
    model = CapacityModel(max_sessions=2)
    state = model.initial_state()
    state = model.admit(state, "request-a").state
    state = model.admit(state, "request-b").state
    state = model.replace(
        state,
        SessionRecord(
            request_id="request-a",
            terminal="completed",
            attached=False,
            worker_done=True,
            cleanup_count=1,
        ),
    )

    admitted = model.admit(state, "request-c")

    assert admitted.code == "admitted"
    assert [session.request_id for session in admitted.state.sessions] == [
        "request-b",
        "request-c",
    ]
    assert admitted.state.runtime_starts == 3


def test_cancelled_session_is_not_evicted_until_worker_cleanup_finishes():
    model = CapacityModel(max_sessions=1)
    state = model.initial_state()
    state = model.admit(state, "request-a").state
    state = model.replace(
        state,
        SessionRecord(
            request_id="request-a",
            terminal="cancelled",
            attached=False,
            worker_done=False,
            cleanup_count=0,
        ),
    )

    blocked = model.admit(state, "request-b")
    assert blocked.code == "gateway_capacity_exhausted"

    cleaned = model.replace(
        blocked.state,
        SessionRecord(
            request_id="request-a",
            terminal="cancelled",
            attached=False,
            worker_done=True,
            cleanup_count=1,
        ),
    )
    admitted = model.admit(cleaned, "request-b")

    assert admitted.code == "admitted"
    assert admitted.state.sessions[0].request_id == "request-b"
