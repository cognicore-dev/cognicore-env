import pytest
import os
import uuid
import json
from unittest.mock import patch, MagicMock

from cognicore.integrations.chatgpt import (
    cognicore_record_experience,
    cognicore_recall_experience,
    cognicore_verify_experience,
    cognicore_check_experience,
    get_backend_for_user
)

def _mock_ctx():
    return MagicMock()

def test_integration_tenant_isolation():
    uid_a = str(uuid.uuid4())
    uid_b = str(uuid.uuid4())
    
    # User A records an experience
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        res_record_a = cognicore_record_experience(
            task="Setup auth",
            problem="Users cannot login",
            solution="Add JWT token validation",
            ctx=_mock_ctx(),
            why_it_worked="Properly verifies issuer and audience",
            attempts=[]
        )
        data_a = json.loads(res_record_a)
        assert data_a["status"] == "recorded"
        exp_id_a = data_a["experience_id"]
        
        # User A recalls the experience
        res_recall_a = cognicore_recall_experience(
            query="auth login",
            ctx=_mock_ctx()
        )
        data_recall_a = json.loads(res_recall_a)
        assert data_recall_a["total_candidates"] >= 1
        assert any(e["experience_id"] == exp_id_a for e in data_recall_a["experiences"])
        
    # User B tries to recall User A's experience
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_b):
        res_recall_b = cognicore_recall_experience(
            query="auth login",
            ctx=_mock_ctx()
        )
        data_recall_b = json.loads(res_recall_b)
        # Should be empty for User B since B has a different isolated DB
        assert not any(e["experience_id"] == exp_id_a for e in data_recall_b["experiences"])
        
        # User B records their own experience
        res_record_b = cognicore_record_experience(
            task="Fix database",
            problem="Missing index",
            solution="Added index on user_id",
            ctx=_mock_ctx(),
            attempts=[]
        )
        data_b = json.loads(res_record_b)
        exp_id_b = data_b["experience_id"]
        
        # Verify B can see B's experience
        res_recall_b2 = cognicore_recall_experience(
            query="database index",
            ctx=_mock_ctx()
        )
        data_recall_b2 = json.loads(res_recall_b2)
        assert any(e["experience_id"] == exp_id_b for e in data_recall_b2["experiences"])
        
    # Verify A cannot see B's experience
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        res_recall_a2 = cognicore_recall_experience(
            query="database index",
            ctx=_mock_ctx()
        )
        data_recall_a2 = json.loads(res_recall_a2)
        assert not any(e["experience_id"] == exp_id_b for e in data_recall_a2["experiences"])

def test_integration_verify_experience():
    uid = str(uuid.uuid4())
    
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid):
        res_record = cognicore_record_experience(
            task="Test Verification",
            problem="Needs verification",
            solution="Verified it",
            ctx=_mock_ctx(),
        )
        exp_id = json.loads(res_record)["experience_id"]
        
        res_verify = cognicore_verify_experience(
            experience_id=exp_id,
            is_valid=True,
            evidence="All tests passed",
            ctx=_mock_ctx()
        )
        
        data_verify = json.loads(res_verify)
        assert data_verify["status"] == "verified"
        assert data_verify["passed"] is True

def test_integration_check_experience():
    uid = str(uuid.uuid4())
    
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid):
        res_record = cognicore_record_experience(
            task="Test Check",
            problem="Needs checking",
            solution="Checked it",
            ctx=_mock_ctx(),
        )
        exp_id = json.loads(res_record)["experience_id"]
        
        res_check = cognicore_check_experience(
            experience_id=exp_id,
            ctx=_mock_ctx()
        )
        
        data_check = json.loads(res_check)
        assert data_check["status"] == "found"
        
        res_check_missing = cognicore_check_experience(
            experience_id="non-existent-id",
            ctx=_mock_ctx()
        )
        
        data_check_missing = json.loads(res_check_missing)
        assert data_check_missing["status"] == "not_found"

def test_isolation_injection_attempt():
    uid_a = str(uuid.uuid4())
    uid_b = str(uuid.uuid4())
    
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        # User A tries to inject B's UUID into the query
        res_recall = cognicore_recall_experience(
            query=f"query {uid_b}",
            ctx=_mock_ctx()
        )
        
        # It shouldn't crash, but it should just execute against A's DB
        data_recall = json.loads(res_recall)
        assert "experiences" in data_recall
        
        # Verify A's DB path is used
        db = get_backend_for_user(uid_a)
        assert str(db.db_path).endswith(f"memory_{uid_a}.db")
from cognicore.integrations.chatgpt import cognicore_delete_all_data

def test_integration_delete_data():
    uid = str(uuid.uuid4())
    
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid):
        # 1. Record an experience
        res_record = cognicore_record_experience(
            task="Task to delete",
            problem="Problem to delete",
            solution="Solution to delete",
            ctx=_mock_ctx(),
        )
        
        # Verify it exists
        res_check_before = cognicore_recall_experience(
            query="delete",
            ctx=_mock_ctx()
        )
        assert json.loads(res_check_before)["total_candidates"] >= 1
        
        # 2. Delete data
        res_delete = cognicore_delete_all_data(ctx=_mock_ctx())
        data_delete = json.loads(res_delete)
        assert data_delete["status"] == "deleted"
        
        # 3. Verify it is gone
        res_check_after = cognicore_recall_experience(
            query="delete",
            ctx=_mock_ctx()
        )
        assert json.loads(res_check_after)["total_candidates"] == 0

def test_integration_delete_isolation():
    uid_a = str(uuid.uuid4())
    uid_b = str(uuid.uuid4())
    
    # User A and B both record experiences
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        cognicore_record_experience(task="A task", problem="A problem", solution="A solution", ctx=_mock_ctx())
    
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_b):
        cognicore_record_experience(task="B task", problem="B problem", solution="B solution", ctx=_mock_ctx())
        
    # User A deletes their data
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        cognicore_delete_all_data(ctx=_mock_ctx())
        
    # Verify User A data is gone
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        res_a = cognicore_recall_experience(query="task", ctx=_mock_ctx())
        assert json.loads(res_a)["total_candidates"] == 0
        
    # Verify User B data STILL EXISTS (User A's delete didn't affect B)
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_b):
        res_b = cognicore_recall_experience(query="task", ctx=_mock_ctx())
        assert json.loads(res_b)["total_candidates"] >= 1

def test_delete_isolation_injection_attempt():
    uid_a = str(uuid.uuid4())
    uid_b = str(uuid.uuid4())
    
    # User B records experience
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_b):
        cognicore_record_experience(task="B task", problem="B problem", solution="B solution", ctx=_mock_ctx())
        
    # User A tries to maliciously pass B's UUID to delete it
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_a):
        # We prove the tool has no parameters where A could inject B's UUID
        import inspect
        sig = inspect.signature(cognicore_delete_all_data)
        assert "user_uuid" not in sig.parameters
        assert "db_path" not in sig.parameters
        
        # User A triggers delete
        cognicore_delete_all_data(ctx=_mock_ctx())
        
    # Verify User B data STILL EXISTS 
    with patch("cognicore.integrations.chatgpt.require_auth", return_value=uid_b):
        res_b = cognicore_recall_experience(query="task", ctx=_mock_ctx())
        assert json.loads(res_b)["total_candidates"] >= 1
