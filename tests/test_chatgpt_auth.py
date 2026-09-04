import pytest
import os
import uuid
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

from cognicore.integrations.auth import validate_token, AuthError, get_jwks_client
from cognicore.integrations.chatgpt import get_backend_for_user, cognicore_recall_experience, cognicore_record_experience

@pytest.fixture(scope="module")
def rs256_keys():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    public_key = private_key.public_key()
    
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    numbers = public_key.public_numbers()
    def int_to_base64(n):
        import base64
        b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
        return base64.urlsafe_b64encode(b).decode('ascii').rstrip('=')
    
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test_key_1",
                "use": "sig",
                "n": int_to_base64(numbers.n),
                "e": int_to_base64(numbers.e),
                "alg": "RS256"
            }
        ]
    }
    
    return private_pem, jwks

class MockJWKClient:
    def __init__(self, public_key_pem):
        self.public_key_pem = public_key_pem
    def get_signing_key_from_jwt(self, token):
        class MockKey:
            key = self.public_key_pem
        return MockKey()

@pytest.fixture
def jwks_client(rs256_keys):
    from jwt import PyJWKClient
    client = PyJWKClient("https://test.supabase.co/auth/v1/.well-known/jwks.json", cache_keys=True)
    client.fetch_data = MagicMock(return_value=rs256_keys[1])
    return client

@pytest.fixture
def supabase_url():
    return "https://test.supabase.co"

def create_test_jwt(private_key, supabase_url, exp_delta_seconds=3600, aud="authenticated", sub=None, alg="RS256", headers=None):
    if sub is None:
        sub = str(uuid.uuid4())
    payload = {
        "iss": f"{supabase_url}/auth/v1",
        "aud": aud,
        "sub": sub,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=exp_delta_seconds)
    }
    hdrs = {"kid": "test_key_1"}
    if headers:
        hdrs.update(headers)
    return jwt.encode(payload, private_key, algorithm=alg, headers=hdrs)

def test_valid_token(rs256_keys, jwks_client, supabase_url):
    uid = str(uuid.uuid4())
    token = create_test_jwt(rs256_keys[0], supabase_url, sub=uid)
    res = validate_token(f"Bearer {token}", supabase_url, jwks_client)
    assert res == uid

def test_missing_header(supabase_url, jwks_client):
    with pytest.raises(AuthError, match="missing authorization header"):
        validate_token(None, supabase_url, jwks_client)

def test_malformed_header(rs256_keys, supabase_url, jwks_client):
    token = create_test_jwt(rs256_keys[0], supabase_url)
    with pytest.raises(AuthError, match="malformed authorization header"):
        validate_token(token, supabase_url, jwks_client) # missing Bearer entirely
    
    with pytest.raises(AuthError, match="malformed authorization header"):
        validate_token(f"Bearer {token} extra", supabase_url, jwks_client)
        
    with pytest.raises(AuthError, match="malformed authorization header"):
        validate_token("Bearer", supabase_url, jwks_client)

def test_invalid_signature(rs256_keys, jwks_client, supabase_url):
    wrong_keys = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8, encryption_algorithm=serialization.NoEncryption()
    )
    token = create_test_jwt(wrong_keys, supabase_url)
    with pytest.raises(AuthError, match="invalid signature"):
        validate_token(f"Bearer {token}", supabase_url, jwks_client)

def test_expired_token(rs256_keys, jwks_client, supabase_url):
    token = create_test_jwt(rs256_keys[0], supabase_url, exp_delta_seconds=-10)
    with pytest.raises(AuthError, match="expired jwt"):
        validate_token(f"Bearer {token}", supabase_url, jwks_client)

def test_incorrect_issuer(rs256_keys, jwks_client):
    token = create_test_jwt(rs256_keys[0], "https://wrong.supabase.co")
    with pytest.raises(AuthError, match="incorrect issuer"):
        validate_token(f"Bearer {token}", "https://test.supabase.co", jwks_client)

def test_incorrect_audience(rs256_keys, jwks_client, supabase_url):
    token = create_test_jwt(rs256_keys[0], supabase_url, aud="wrong_aud")
    with pytest.raises(AuthError, match="incorrect audience"):
        validate_token(f"Bearer {token}", supabase_url, jwks_client)

def test_missing_sub(rs256_keys, jwks_client, supabase_url):
    payload = {
        "iss": f"{supabase_url}/auth/v1",
        "aud": "authenticated",
        "exp": datetime.now(timezone.utc) + timedelta(seconds=3600)
    }
    token = jwt.encode(payload, rs256_keys[0], algorithm="RS256", headers={"kid": "test_key_1"})
    with pytest.raises(AuthError, match="missing sub claim"):
        validate_token(f"Bearer {token}", supabase_url, jwks_client)

def test_non_uuid_sub(rs256_keys, jwks_client, supabase_url):
    token = create_test_jwt(rs256_keys[0], supabase_url, sub="non-uuid-string")
    with pytest.raises(AuthError, match="invalid/non-UUID sub claim"):
        validate_token(f"Bearer {token}", supabase_url, jwks_client)

def test_path_traversal_sub(rs256_keys, jwks_client, supabase_url):
    token = create_test_jwt(rs256_keys[0], supabase_url, sub="../../../etc/passwd")
    with pytest.raises(AuthError, match="invalid/non-UUID sub claim"):
        validate_token(f"Bearer {token}", supabase_url, jwks_client)

def test_two_different_users(rs256_keys, jwks_client, supabase_url):
    uid1 = str(uuid.uuid4())
    uid2 = str(uuid.uuid4())
    res1 = validate_token(f"Bearer {create_test_jwt(rs256_keys[0], supabase_url, sub=uid1)}", supabase_url, jwks_client)
    res2 = validate_token(f"Bearer {create_test_jwt(rs256_keys[0], supabase_url, sub=uid2)}", supabase_url, jwks_client)
    assert res1 != res2

def test_hs256_rejected(rs256_keys, jwks_client, supabase_url):
    payload = {
        "iss": f"{supabase_url}/auth/v1",
        "aud": "authenticated",
        "sub": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(seconds=3600)
    }
    token = jwt.encode(payload, b'random_secret_bytes', algorithm="HS256", headers={"kid": "test_key_1"})
    with pytest.raises(AuthError, match="invalid JWT"):
        validate_token(f"Bearer {token}", supabase_url, jwks_client)

def test_jwks_caching(rs256_keys, supabase_url):
    from jwt import PyJWKClient
    client = PyJWKClient(f"{supabase_url}/auth/v1/.well-known/jwks.json", cache_keys=True)
    client.fetch_data = MagicMock(return_value=rs256_keys[1])
    
    uid = str(uuid.uuid4())
    token = create_test_jwt(rs256_keys[0], supabase_url, sub=uid)
    
    res1 = validate_token(f"Bearer {token}", supabase_url, client)
    assert res1 == uid
    assert client.fetch_data.call_count == 1
    
    res2 = validate_token(f"Bearer {token}", supabase_url, client)
    assert res2 == uid
    assert client.fetch_data.call_count == 1

def test_tenant_isolation_db_path():
    uid1 = str(uuid.uuid4())
    uid2 = str(uuid.uuid4())
    
    db1 = get_backend_for_user(uid1)
    db2 = get_backend_for_user(uid2)
    
    p1 = str(db1.db_path)
    p2 = str(db2.db_path)
    
    assert p1 != p2
    assert uid1 in p1
    assert "memory_" + uid1 + ".db" in p1

def test_unexpected_exception_handling(rs256_keys, supabase_url):
    class BadClient:
        def get_signing_key_from_jwt(self, token):
            raise RuntimeError("Database connection lost!")
            
    token = create_test_jwt(rs256_keys[0], supabase_url)
    with pytest.raises(AuthError, match="authentication failed"):
        validate_token(f"Bearer {token}", supabase_url, BadClient())

def test_tenant_isolation_tool_arguments_bypass():
    uid_a = str(uuid.uuid4())
    uid_b = str(uuid.uuid4())
    
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        with patch("cognicore.integrations.chatgpt.get_backend_for_user") as mock_backend:
            mock_backend.return_value = MagicMock()
            
            malicious_query = f"query with {uid_b} or ../memory_{uid_b}.db"
            mock_ctx = MagicMock()
            
            cognicore_recall_experience(malicious_query, mock_ctx)
            
            # Verify get_backend_for_user was ONLY called with A's UUID
            mock_backend.assert_called_once_with(uid_a)

            import inspect
            sig = inspect.signature(cognicore_recall_experience)
            assert "user_uuid" not in sig.parameters
            assert "db_path" not in sig.parameters
