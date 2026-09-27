"""The shared vectors: every value the operator also recomputes."""

import json
from pathlib import Path
from typing import Any

from airdress_home.codes import (
    MachineKey,
    b64url,
    code_matches,
    confirmation_code,
    verify_answer,
)
from airdress_home.frames import signed_bytes, verify
from airdress_home.httpsig import sign_request

V: dict[str, Any] = json.loads((Path(__file__).parent / "vectors" / "vectors.json").read_text())


def test_machine_key() -> None:
    m = V["machine"]
    key = MachineKey.from_b64(m["seed"])
    assert key.public_b64 == m["public_key"]
    assert key.fingerprint == m["fingerprint"]
    assert key.kid == m["kid"]
    assert key.proof(m["device_code"]) == m["proof"]


def test_operator_answer_and_confirmation_code() -> None:
    machine = MachineKey.from_b64(V["machine"]["seed"])
    o = V["operator"]
    op = MachineKey.from_b64(o["seed"])
    assert verify_answer(op.public, o["answer_proof"], o["origin"], o["user_code"], machine.public)
    assert verify_answer(
        op.public, o["answer_proof"], o["origin"].upper() + "/", o["user_code"], machine.public
    ), "the origin is normalized"
    assert not verify_answer(
        op.public, o["answer_proof"], "https://evil.example", o["user_code"], machine.public
    )
    code = confirmation_code(op.public, machine.public, o["user_code"])
    assert code == o["confirmation_code"]
    assert len(code) == 24
    assert code_matches(code.lower().replace("-", " "), code)


def test_signed_requests_are_byte_identical() -> None:
    key = MachineKey.from_b64(V["machine"]["seed"])
    for case in V["httpsig"]:
        headers = {"Content-Type": case["content_type"]} if case["content_type"] else {}
        got = sign_request(
            key,
            V["machine"]["keyid"],
            case["method"],
            case["target_uri"],
            headers,
            case["body"].encode(),
            created=case["created"],
            nonce=case["nonce"],
            lifetime=case["lifetime"],
        )
        assert got == case["headers"]


def test_frames_verify_under_the_pinned_key_only() -> None:
    op = MachineKey.from_b64(V["operator"]["seed"])
    for f in V["frames"]:
        assert b64url(op.sign(signed_bytes(f["type"], f["frame"]))) == f["sig"]
        frame = verify({"type": f["type"], "frame": f["frame"], "sig": f["sig"]}, op.public)
        assert frame.seq == 2
        other = MachineKey(bytes(32)).public
        try:
            verify({"type": f["type"], "frame": f["frame"], "sig": f["sig"]}, other)
        except Exception as e:
            assert "does not verify" in str(e)
        else:
            raise AssertionError("a frame verified under another key")
