import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.integration.conversations_identity import assert_no_signed_tokens, b64


@pytest.mark.parametrize("service", [0, 1, 2])
def test_recipe_rejects_any_signed_delegation_in_each_service_log(service):
    key = Ed25519PrivateKey.generate()
    logs = ["api_request request_id=" + "a" * 32 + " status=200"] * 3
    assert_no_signed_tokens(*logs)
    header = b64(json.dumps({"alg": "EdDSA", "kid": "synthetic-recipe"}).encode())
    for jti in ("chat-attempt-one", "chat-attempt-two"):
        unsigned = header + "." + b64(json.dumps({"jti": jti, "sub": "synthetic-user"}).encode())
        assertion = unsigned + "." + b64(key.sign(unsigned.encode()))
        logs[service] = "Authorization: Bearer " + assertion
        with pytest.raises(AssertionError, match="Signed token found in service logs") as error:
            assert_no_signed_tokens(*logs)
        assert assertion not in str(error.value)
