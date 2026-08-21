import logging
import uuid
from typing import Any, Dict, List

from cognicore.experience.schema import (
    Attempt,
    AttemptOutcome,
    EnvironmentContext,
    EvidenceRecord,
    RepositoryContext,
    StructuredExperience,
    VerificationStatus,
)

logger = logging.getLogger("cognicore.experience.extractor")


class ExperienceExtractor:
    """
    Extracts a StructuredExperience from a completed coding task/session.
    
    This converts a raw session payload (which might contain the history of
    actions, commands, tests, and outcomes) into a formalized StructuredExperience.
    """
    
    def extract(self, session_data: Dict[str, Any]) -> StructuredExperience:
        """
        Parses session data and structures it into a StructuredExperience.
        
        Args:
            session_data: A dictionary containing the session history, attempts,
                          outcomes, and environment context.
                          
        Returns:
            A StructuredExperience object ready to be verified or stored.
        """
        task = session_data.get("task", "")
        problem = session_data.get("problem", "")
        
        # Extract attempts
        attempts = []
        for attempt_data in session_data.get("attempts", []):
            attempts.append(
                Attempt(
                    approach=attempt_data.get("approach", ""),
                    outcome=attempt_data.get("outcome", AttemptOutcome.UNKNOWN.value),
                    reason=attempt_data.get("reason", ""),
                    evidence=attempt_data.get("evidence", ""),
                )
            )
            
        solution = session_data.get("solution", "")
        why_it_worked = session_data.get("why_it_worked", "")
        
        # Extract environment and repository info
        repo_data = session_data.get("repository", {})
        repository = RepositoryContext(
            repo_id=repo_data.get("repo_id", ""),
            commit=repo_data.get("commit", ""),
            branch=repo_data.get("branch", ""),
            affected_files=repo_data.get("affected_files", []),
        )
        
        env_data = session_data.get("environment", {})
        environment = EnvironmentContext(
            python_version=env_data.get("python_version", ""),
            os=env_data.get("os", ""),
            framework=env_data.get("framework", ""),
            framework_version=env_data.get("framework_version", ""),
            dependencies=env_data.get("dependencies", {}),
        )
        
        # Extract evidence (if any was pre-collected during the session)
        evidence_records = []
        for ev in session_data.get("verification_evidence", []):
            evidence_records.append(
                EvidenceRecord(
                    command=ev.get("command", ""),
                    exit_code=int(ev.get("exit_code", 0)),
                    stdout_hash=ev.get("stdout_hash", ""),
                    timestamp=ev.get("timestamp", ""),
                    commit=ev.get("commit", ""),
                )
            )
            
        experience_id = session_data.get("experience_id", f"exp_{uuid.uuid4().hex[:12]}")
        
        experience = StructuredExperience(
            experience_id=experience_id,
            task=task,
            problem=problem,
            attempts=attempts,
            solution=solution,
            why_it_worked=why_it_worked,
            verification_status=VerificationStatus.CANDIDATE.value,
            verification_evidence=evidence_records,
            source_agent=session_data.get("source_agent", "unknown"),
            source_session=session_data.get("source_session", "unknown"),
            repository=repository,
            environment=environment,
        )
        
        return experience
