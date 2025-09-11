"""
Coordination Tracker for MassGen Orchestrator

This module provides comprehensive tracking of agent coordination events,
state transitions, and context sharing. It's integrated into the orchestrator
to capture the complete coordination flow for visualization and analysis.

The new approach is principled: we simply record what happens as it happens,
without trying to infer or manage state transitions. The orchestrator tells
us exactly what occurred and when.
"""

import time
import json
from datetime import datetime
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from .utils import AgentStatus, ActionType

@dataclass
class CoordinationEvent:
    """A single coordination event with timestamp."""
    timestamp: float
    event_type: str
    agent_id: Optional[str] = None
    details: str = ""
    context: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "agent_id": self.agent_id,
            "details": self.details,
            "context": self.context
        }

@dataclass 
class AgentAnswer:
    """Represents an answer from an agent."""
    agent_id: str
    content: str
    timestamp: float
    is_final: bool = False
    
    @property
    def label(self) -> str:
        """Auto-generate label based on answer properties."""
        # This will be set by the tracker when it knows agent order
        return getattr(self, '_label', 'unknown')
    
    @label.setter
    def label(self, value: str):
        self._label = value

@dataclass
class AgentVote:
    """Represents a vote from an agent."""
    voter_id: str
    voted_for: str  # Real agent ID like "gpt5nano_1"
    voted_for_label: str  # Answer label like "agent1.1"
    voter_anon_id: str  # Anonymous voter ID like "agent1"
    reason: str
    timestamp: float
    available_answers: List[str]  # Available answer labels like ["agent1.1", "agent2.1"]

class CoordinationTracker:
    """
    Principled coordination tracking that simply records what happens.
    
    The orchestrator tells us exactly what occurred and when, without
    us having to infer or manage complex state transitions.
    """
    
    def __init__(self):
        # Event log - chronological record of everything that happens
        self.events: List[CoordinationEvent] = []
        
        # Answer tracking with labeling
        self.answers_by_agent: Dict[str, List[AgentAnswer]] = {}  # agent_id -> list of answers
        self.all_answers: Dict[str, str] = {}  # label -> content mapping for easy lookup
        
        # Vote tracking
        self.votes: List[AgentVote] = []
        
        # Coordination iteration tracking
        self.current_iteration: int = 0
        self.agent_rounds: Dict[str, int] = {}  # Per-agent round tracking - increments when restart completed
        self.agent_round_context: Dict[str, Dict[int, List[str]]] = {}  # What context each agent had in each round
        self.iteration_available_labels: List[str] = []  # Frozen snapshot of available answer labels for current iteration
        
        # Restart tracking - track pending restarts per agent
        self.pending_agent_restarts: Dict[str, bool] = {}  # agent_id -> is restart pending
        
        # Session info
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.agent_ids: List[str] = []
        self.pending_restarts: Set[str] = set()  # Agents that need to restart for current round
        self.final_winner: Optional[str] = None
        self.final_context: Optional[Dict[str, Any]] = None  # Context provided to final agent
        self.is_final_round: bool = False  # Track if we're in the final presentation round
        self.user_prompt: Optional[str] = None  # Store the initial user prompt
        
        # Canonical ID/Label mappings - coordination tracker is the single source of truth
        self.agent_id_to_anon: Dict[str, str] = {}  # gpt5nano_1 -> agent1
        self.anon_to_agent_id: Dict[str, str] = {}  # agent1 -> gpt5nano_1
        self.agent_context_labels: Dict[str, List[str]] = {}  # Track what labels each agent can see
        
        # Answer formatting settings
        self.preview_length = 150  # Default preview length for answers
        
        # Snapshot mapping - tracks filesystem snapshots for answers/votes
        self.snapshot_mappings: Dict[str, Dict[str, Any]] = {}  # label/vote_id -> snapshot info

    def initialize_session(self, agent_ids: List[str], user_prompt: Optional[str] = None):
        """Initialize a new coordination session."""
        self.start_time = time.time()
        self.agent_ids = agent_ids.copy()
        self.answers_by_agent = {aid: [] for aid in agent_ids}
        self.user_prompt = user_prompt
        
        # Initialize per-agent round tracking
        self.agent_rounds = {aid: 0 for aid in agent_ids}
        self.agent_round_context = {aid: {0: []} for aid in agent_ids}  # Each agent starts in round 0 with empty context
        self.pending_agent_restarts = {aid: False for aid in agent_ids}
        
        # Set up canonical mappings - coordination tracker is single source of truth
        self.agent_id_to_anon = {aid: f"agent{i+1}" for i, aid in enumerate(agent_ids)}
        self.anon_to_agent_id = {v: k for k, v in self.agent_id_to_anon.items()}
        self.agent_context_labels = {aid: [] for aid in agent_ids}
        
        self._add_event("session_start", None, f"Started with agents: {agent_ids}")

    # Canonical mapping utility methods
    def get_anonymous_id(self, agent_id: str) -> str:
        """Get anonymous ID (agent1, agent2) for a full agent ID."""
        return self.agent_id_to_anon.get(agent_id, agent_id)
    
    def get_full_agent_id(self, anon_id: str) -> str:
        """Get full agent ID for an anonymous ID."""
        return self.anon_to_agent_id.get(anon_id, anon_id)
    
    def get_agent_context_labels(self, agent_id: str) -> List[str]:
        """Get the answer labels this agent can currently see."""
        return self.agent_context_labels.get(agent_id, []).copy()
    
    def get_latest_answer_label(self, agent_id: str) -> Optional[str]:
        """Get the latest answer label for an agent."""
        if agent_id in self.answers_by_agent and self.answers_by_agent[agent_id]:
            return self.answers_by_agent[agent_id][-1].label
        return None
    
    def get_agent_round(self, agent_id: str) -> int:
        """Get the current round for a specific agent."""
        return self.agent_rounds.get(agent_id, 0)
    
    def get_max_round(self) -> int:
        """Get the highest round number across all agents (for backward compatibility)."""
        return max(self.agent_rounds.values()) if self.agent_rounds else 0
    
    @property
    def current_round(self) -> int:
        """Backward compatibility property that returns the maximum round across all agents."""
        return self.get_max_round()

    def start_new_iteration(self):
        """Start a new coordination iteration."""
        self.current_iteration += 1
        
        # Capture available answer labels at start of this iteration (freeze snapshot)
        self.iteration_available_labels = []
        for agent_id, answers_list in self.answers_by_agent.items():
            if answers_list:  # Agent has provided at least one answer
                latest_answer = answers_list[-1]  # Get most recent answer
                self.iteration_available_labels.append(latest_answer.label)  # e.g., "agent1.1"
        
        self._add_event("iteration_start", None, f"Starting coordination iteration {self.current_iteration}", 
                       {"iteration": self.current_iteration, "available_answers": self.iteration_available_labels.copy()})

    def end_iteration(self, reason: str, details: Dict[str, Any] = None):
        """Record how an iteration ended."""
        context = {
            "iteration": self.current_iteration,
            "end_reason": reason,
            "available_answers": self.iteration_available_labels.copy()
        }
        if details:
            context.update(details)
            
        self._add_event("iteration_end", None, f"Iteration {self.current_iteration} ended: {reason}", context)

    def set_user_prompt(self, prompt: str):
        """Set or update the user prompt."""
        self.user_prompt = prompt

    def change_status(self, agent_id: str, new_status: AgentStatus):
        """Record when an agent changes status."""
        self._add_event("status_change", agent_id, f"Changed to status: {new_status.value}")

    def track_agent_context(self, agent_id: str, answers: Dict[str, str], conversation_history: Optional[Dict[str, Any]] = None, agent_full_context: Optional[str] = None, snapshot_dir: Optional[str] = None):
        """Record when an agent receives context.

        Args:
            agent_id: The agent receiving context
            answers: Dict of agent_id -> answer content
            conversation_history: Optional conversation history
            agent_full_context: Optional full context string/dict to save
            snapshot_dir: Optional directory path to save context.txt
        """
        # Convert full agent IDs to their corresponding answer labels using canonical mappings
        answer_labels = []
        for answering_agent_id in answers.keys():
            if answering_agent_id in self.answers_by_agent and self.answers_by_agent[answering_agent_id]:
                # Get the most recent answer's label
                latest_answer = self.answers_by_agent[answering_agent_id][-1]
                answer_labels.append(latest_answer.label)
        
        # Update this agent's context labels using canonical mapping
        self.agent_context_labels[agent_id] = answer_labels.copy()
        
        # Use anonymous agent IDs for the event context
        anon_answering_agents = [self.agent_id_to_anon.get(aid, aid) for aid in answers.keys()]
        
        context = {
            "available_answers": anon_answering_agents,  # Anonymous IDs for backward compat
            "available_answer_labels": answer_labels.copy(),  # Store actual labels in event
            "answer_count": len(answers),
            "has_conversation_history": bool(conversation_history)
        }
        self._add_event("context_received", agent_id, 
                       f"Received context with {len(answers)} answers", context)

    def track_restart_signal(self, triggering_agent: str, agents_restarted: List[str]):
        """Record when a restart is triggered - but don't increment rounds yet."""
        # Mark affected agents as having pending restarts
        for agent_id in agents_restarted:
            if True:  # agent_id != triggering_agent:  # Triggering agent doesn't restart themselves
                self.pending_agent_restarts[agent_id] = True
        
        # Log restart event (no round increment yet)
        context = {"affected_agents": agents_restarted, "triggering_agent": triggering_agent}
        self._add_event("restart_triggered", triggering_agent, 
                       f"Triggered restart affecting {len(agents_restarted)} agents", context)

    def complete_agent_restart(self, agent_id: str):
        """Record when an agent has completed its restart and increment their round.
        
        Args:
            agent_id: The agent that completed restart
        """
        print(f"DEBUG: complete_agent_restart called for {agent_id}")
        print(f"DEBUG: pending_agent_restarts = {self.pending_agent_restarts}")
        print(f"DEBUG: agent_rounds before = {self.agent_rounds}")
        
        if not self.pending_agent_restarts.get(agent_id, False):
            # This agent wasn't pending a restart, nothing to do
            print(f"DEBUG: {agent_id} was not pending restart, skipping")
            return
            
        # Mark restart as completed
        self.pending_agent_restarts[agent_id] = False
        
        # Increment this agent's round
        self.agent_rounds[agent_id] += 1
        new_round = self.agent_rounds[agent_id]
        
        print(f"DEBUG: {agent_id} round incremented to {new_round}")
        
        # Store the context this agent will work with in their new round
        if agent_id not in self.agent_round_context:
            self.agent_round_context[agent_id] = {}
        
        # Log restart completion
        context = {
            "agent_round": new_round,
        }
        self._add_event("restart_completed", agent_id, 
                       f"Completed restart - now in round {new_round}", context)

    def add_agent_answer(self, agent_id: str, answer: str, snapshot_timestamp: Optional[str] = None):
        """Record when an agent provides a new answer.
        
        Args:
            agent_id: ID of the agent
            answer: The answer content
            snapshot_timestamp: Timestamp of the filesystem snapshot (if any)
        """
        # Create answer object
        agent_answer = AgentAnswer(
            agent_id=agent_id,
            content=answer,
            timestamp=time.time(),
            is_final=False
        )
        
        # Auto-generate label based on agent position and answer count
        agent_num = self.agent_ids.index(agent_id) + 1
        answer_num = len(self.answers_by_agent[agent_id]) + 1
        label = f"agent{agent_num}.{answer_num}"
        agent_answer.label = label
        
        # Store the answer
        self.answers_by_agent[agent_id].append(agent_answer)
        self.all_answers[label] = answer  # Quick lookup by label
        
        # Track snapshot mapping if provided
        if snapshot_timestamp:
            self.snapshot_mappings[label] = {
                "type": "answer",
                "label": label,
                "agent_id": agent_id,
                "timestamp": snapshot_timestamp,
                "iteration": self.current_iteration,
                "round": self.get_agent_round(agent_id),
                "path": f"{agent_id}/{snapshot_timestamp}/answer.txt"
            }
        
        # Record event with label (important info) but no preview (that's for display only)
        context = {"label": label}
        self._add_event("new_answer", agent_id, f"Provided answer {label}", context)

    def add_agent_vote(self, agent_id: str, vote_data: Dict[str, Any], snapshot_timestamp: Optional[str] = None):
        """Record when an agent votes.
        
        Args:
            agent_id: ID of the voting agent
            vote_data: Dictionary with vote information
            snapshot_timestamp: Timestamp of the filesystem snapshot (if any)
        """
        # Handle both "voted_for" and "agent_id" keys (orchestrator uses "agent_id")
        voted_for = vote_data.get("voted_for") or vote_data.get("agent_id", "unknown")
        reason = vote_data.get("reason", "")
        
        # Convert real agent IDs to anonymous IDs and answer labels
        voter_anon_id = self._get_anonymous_agent_id(agent_id)
        
        # Find the voted-for answer label (agent1.1, agent2.1, etc.)
        voted_for_label = "unknown"
        if voted_for in self.agent_ids:
            # Find the latest answer from the voted-for agent at vote time
            voted_agent_answers = self.answers_by_agent.get(voted_for, [])
            if voted_agent_answers:
                voted_for_label = voted_agent_answers[-1].label
        
        # Store the vote
        vote = AgentVote(
            voter_id=agent_id,
            voted_for=voted_for,
            voted_for_label=voted_for_label,
            voter_anon_id=voter_anon_id,
            reason=reason,
            timestamp=time.time(),
            available_answers=self.iteration_available_labels.copy()
        )
        self.votes.append(vote)
        
        # Track snapshot mapping if provided
        if snapshot_timestamp:
            # Create a meaningful vote label similar to answer labels
            agent_num = self.agent_ids.index(agent_id) + 1 if agent_id in self.agent_ids else 0
            vote_num = len([v for v in self.votes if v.voter_id == agent_id])
            vote_label = f"agent{agent_num}.vote{vote_num}"
            
            self.snapshot_mappings[vote_label] = {
                "type": "vote",
                "label": vote_label,
                "agent_id": agent_id,
                "timestamp": snapshot_timestamp,
                "voted_for": voted_for,
                "voted_for_label": voted_for_label,
                "iteration": self.current_iteration,
                "round": self.get_agent_round(agent_id),
                "path": f"{agent_id}/{snapshot_timestamp}/vote.json"
            }
        
        # Record event - only essential info in context
        context = {
            "voted_for": voted_for,  # Real agent ID for compatibility
            "voted_for_label": voted_for_label,  # Answer label for display
            "reason": reason,
            "available_answers": self.iteration_available_labels.copy()
        }
        self._add_event("vote_cast", agent_id, f"Voted for {voted_for_label}", context)

    def set_final_agent(self, agent_id: str, vote_summary: str, all_answers: Dict[str, str]):
        """Record when final agent is selected."""
        self.final_winner = agent_id
        self.final_context = {
            "vote_summary": vote_summary,
            "all_answers": list(all_answers.keys()),
            "answers_for_context": all_answers  # Full answers provided to final agent
        }
        # log this
        print(f"DEBUG: setting final agent {agent_id} with context: {self.final_context}")
        self._add_event("final_agent_selected", agent_id, "Selected as final presenter", self.final_context)

    def set_final_answer(self, agent_id: str, final_answer: str, snapshot_timestamp: Optional[str] = None):
        """Record the final answer presentation.
        
        Args:
            agent_id: ID of the agent
            final_answer: The final answer content
            snapshot_timestamp: Timestamp of the filesystem snapshot (if any)
        """
        # Create final answer object
        final_answer_obj = AgentAnswer(
            agent_id=agent_id,
            content=final_answer,
            timestamp=time.time(),
            is_final=True
        )
        
        # Auto-generate final label
        agent_num = self.agent_ids.index(agent_id) + 1
        label = f"agent{agent_num}.final"
        final_answer_obj.label = label
        
        # Store the final answer
        self.answers_by_agent[agent_id].append(final_answer_obj)
        self.all_answers[label] = final_answer
        
        # Track snapshot mapping if provided
        if snapshot_timestamp:
            self.snapshot_mappings[label] = {
                "type": "final_answer",
                "label": label,
                "agent_id": agent_id,
                "timestamp": snapshot_timestamp,
                "iteration": self.current_iteration,
                "round": self.get_agent_round(agent_id),
                "path": f"final/{agent_id}/answer.txt" if snapshot_timestamp == "final" else f"{agent_id}/{snapshot_timestamp}/answer.txt"
            }
        
        # Record event with label only (no preview)
        context = {"label": label, **(self.final_context or {})}
        self._add_event("final_answer", agent_id, f"Presented final answer {label}", context)

    def start_final_round(self, selected_agent_id: str):
        """Start the final presentation round."""
        self.is_final_round = True
        # Increment the selected agent to a special "final" round
        self.agent_rounds[selected_agent_id] += 1
        final_round = self.agent_rounds[selected_agent_id]
        self.final_winner = selected_agent_id
        
        # Mark winner as starting final presentation
        self.change_status(selected_agent_id, AgentStatus.STREAMING)
        
        self._add_event("final_round_start", selected_agent_id, 
                       f"Starting final presentation round {final_round}", 
                       {"round_type": "final", "final_round": final_round})

    def track_agent_action(self, agent_id: str, action_type, details: str = ""):
        """Track any agent action using ActionType enum."""
        if action_type == ActionType.NEW_ANSWER:
            # For answers, details should be the actual answer content
            self.add_agent_answer(agent_id, details)
        elif action_type == ActionType.VOTE:
            # For votes, details should be vote data dict - but this needs to be handled separately
            # since add_agent_vote expects a dict, not a string
            pass  # Use add_agent_vote directly
        else:
            # For errors, timeouts, cancellations
            action_name = action_type.value.upper()
            self._add_event(f"agent_{action_type.value}", agent_id, f"{action_name}: {details}" if details else action_name)

    def _add_event(self, event_type: str, agent_id: Optional[str], details: str, context: Optional[Dict[str, Any]] = None):
        """Internal method to add an event."""
        # Automatically include current iteration and round in context
        if context is None:
            context = {}
        context = context.copy()  # Don't modify the original
        context["iteration"] = self.current_iteration
        
        # Include agent-specific round if agent_id is provided, otherwise use max round for backward compatibility
        if agent_id:
            context["round"] = self.get_agent_round(agent_id)
        else:
            context["round"] = self.get_max_round()
        
        event = CoordinationEvent(
            timestamp=time.time(),
            event_type=event_type,
            agent_id=agent_id,
            details=details,
            context=context
        )
        self.events.append(event)

    def _end_session(self):
        """Mark the end of the coordination session."""
        self.end_time = time.time()
        duration = self.end_time - (self.start_time or self.end_time)
        self._add_event("session_end", None, f"Session completed in {duration:.1f}s")
    
    def get_all_answers(self) -> Dict[str, str]:
        """Get all answers as a label->content dictionary."""
        return self.all_answers.copy()
    
    def get_answers_for_display(self, max_preview_length: int = 150) -> Dict[str, Dict[str, Any]]:
        """Get answers with preview for display purposes."""
        display_answers = {}
        for label, content in self.all_answers.items():
            preview = content[:max_preview_length] + "..." if len(content) > max_preview_length else content
            display_answers[label] = {
                "content": content,
                "preview": preview,
                "is_final": label.endswith(".final")
            }
        return display_answers
    
    def format_answer_preview(self, content: str, max_length: Optional[int] = None) -> str:
        """Format answer content for display with consistent preview length and ellipsis handling.
        
        Args:
            content: The full answer content
            max_length: Override default preview length (uses self.preview_length if None)
            
        Returns:
            Formatted preview string with ellipsis only if actually truncated
        """
        if not content:
            return "No content"
        
        length = max_length if max_length is not None else self.preview_length
        
        # Only add ellipsis if we're actually truncating
        if len(content) <= length:
            return content
        else:
            return content[:length].rstrip() + "..."
    
    def get_summary(self) -> Dict[str, Any]:
        """Get session summary statistics."""
        duration = (self.end_time or time.time()) - (self.start_time or time.time())
        restart_count = len([e for e in self.events if e.event_type == "restart_triggered"])
        
        return {
            "duration": duration,
            "total_events": len(self.events),
            "total_restarts": restart_count,
            "total_answers": len([label for label in self.all_answers if not label.endswith(".final")]),
            "final_winner": self.final_winner,
            "agent_count": len(self.agent_ids)
        }
    
    def save_coordination_logs(self, log_dir):
        """Save all coordination data and create timeline visualization.
        
        Args:
            log_dir: Directory to save logs
            format_style: "old", "new", or "both" (default)
        """
        try:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            
            # Save raw events
            events_file = log_dir / "coordination_events.json"
            with open(events_file, 'w', encoding='utf-8') as f:
                events_data = [event.to_dict() for event in self.events]
                json.dump(events_data, f, indent=2, default=str)
            
            # Save snapshot mappings to track filesystem snapshots
            if self.snapshot_mappings:
                snapshot_mappings_file = log_dir / "snapshot_mappings.json"
                with open(snapshot_mappings_file, 'w', encoding='utf-8') as f:
                    json.dump(self.snapshot_mappings, f, indent=2, default=str)
            
            # # Save all answers with labels
            # answers_file = log_dir / "answers.json"
            # with open(answers_file, 'w', encoding='utf-8') as f:
            #     # Include both the dictionary and the detailed list
            #     answers_data = {
            #         "all_answers": self.all_answers,  # label -> content mapping
            #         "by_agent": {
            #             agent_id: [
            #                 {
            #                     "label": answer.label,
            #                     "content": answer.content,
            #                     "timestamp": answer.timestamp,
            #                     "is_final": answer.is_final
            #                 } for answer in answers
            #             ] for agent_id, answers in self.answers_by_agent.items()
            #         }
            #     }
            #     json.dump(answers_data, f, indent=2, default=str)
            
            # Individual answer files are now saved by orchestrator with proper timestamps
            # The snapshot_mappings.json file tracks the mapping from labels to filesystem paths
            
            # Save votes
            # if self.votes:
            #     votes_file = log_dir / "votes.json"
            #     with open(votes_file, 'w', encoding='utf-8') as f:
            #         votes_data = [
            #             {
            #                 "voter_id": vote.voter_id,
            #                 "voted_for": vote.voted_for,
            #                 "voted_for_label": vote.voted_for_label,
            #                 "voter_anon_id": vote.voter_anon_id,
            #                 "reason": vote.reason,
            #                 "timestamp": vote.timestamp,
            #                 "available_answers": vote.available_answers
            #             } for vote in self.votes
            #         ]
            #         json.dump(votes_data, f, indent=2, default=str)
            
            # Create round-based timeline based on format preference
            self._create_round_timeline_file(log_dir)
            
            print(f"💾 Coordination logs saved to: {log_dir}")
            
        except Exception as e:
            print(f"Failed to save coordination logs: {e}")
    
    def _get_agent_id_from_label(self, label: str) -> str:
        """Extract agent_id from a label like 'agent1.1' or 'agent2.final'."""
        # Extract agent number from label
        import re
        match = re.match(r'agent(\d+)', label)
        if match:
            agent_num = int(match.group(1))
            if 0 < agent_num <= len(self.agent_ids):
                return self.agent_ids[agent_num - 1]
        return "unknown"
    
    def _create_timeline_file(self, log_dir):
        """Create the text-based timeline visualization file."""
        timeline_file = log_dir / "coordination_timeline.txt"
        timeline_steps = self._generate_timeline_steps()
        summary = self.get_summary()
        
        with open(timeline_file, 'w', encoding='utf-8') as f:
            # Header
            f.write("=" * 120 + "\n")
            f.write("MASSGEN COORDINATION SUMMARY\n")
            f.write("=" * 120 + "\n\n")
            
            # Description
            f.write("DESCRIPTION:\n")
            f.write("This timeline shows the coordination flow between multiple AI agents.\n")
            f.write("• Context: Previous answers supplied to each agent as background\n")
            f.write("• User Query: The original task/question is always included in each agent's prompt\n")
            f.write("• Coordination: Agents build upon each other's work through answer sharing and voting\n\n")
            
            # Agent mapping
            f.write("AGENT MAPPING:\n")
            for i, agent_id in enumerate(self.agent_ids):
                f.write(f"  Agent{i+1} = {agent_id}\n")
            f.write("\n")
            
            # Summary stats
            f.write(f"Duration: {summary['duration']:.1f}s | ")
            f.write(f"Events: {summary['total_events']} | ")
            f.write(f"Restarts: {summary['total_restarts']} | ")
            f.write(f"Winner: {summary['final_winner'] or 'None'}\n\n")
            
            # Timeline table
            self._write_timeline_table(f, timeline_steps)
            
            # Footer
            f.write("\nKEY CONCEPTS:\n")
            f.write("-" * 20 + "\n")
            f.write("• NEW ANSWER: Agent provides a new response given the initial user query and the supplied context\n")
            f.write("• RESTART: Agent restarts with new context\n")
            f.write("• VOTING: Agents choose best answer from available options\n")
            f.write("• FINAL: Winning agent presents synthesized response\n")
            f.write("• ERROR/TIMEOUT: Agent encounters issues\n\n")
            f.write("=" * 120 + "\n")
    
    def _create_simplified_timeline_file(self, log_dir):
        """Create a simplified timeline that groups each agent's actions clearly."""
        simplified_file = log_dir / "coordination_timeline_simplified.txt"
        
        with open(simplified_file, 'w', encoding='utf-8') as f:
            # Header
            f.write("=" * 100 + "\n")
            f.write("MASSGEN COORDINATION TIMELINE (SIMPLIFIED)\n")
            f.write("=" * 100 + "\n\n")
            
            # Description
            f.write("DESCRIPTION:\n")
            f.write("This shows what each agent did during coordination, grouped by action type.\n")
            f.write("Events are shown in chronological order with clear agent attribution.\n\n")
            
            # Agent mapping
            f.write("AGENT MAPPING:\n")
            for i, agent_id in enumerate(self.agent_ids):
                f.write(f"  Agent{i+1} = {agent_id}\n")
            f.write("\n")
            
            # Sort events by timestamp
            sorted_events = sorted(self.events, key=lambda e: e.timestamp)
            
            # Process events grouped by iteration
            f.write("COORDINATION ITERATIONS:\n")
            f.write("-" * 50 + "\n")
            
            # Group events by iteration
            events_by_iteration = {}
            for event in sorted_events:
                # Skip session start/end events
                if event.event_type in ["session_start", "session_end"]:
                    continue
                    
                iteration = event.context.get("iteration", 0) if event.context else 0
                if iteration not in events_by_iteration:
                    events_by_iteration[iteration] = []
                events_by_iteration[iteration].append(event)
            
            # Track current status of all agents
            agent_current_status = {aid: "IDLE" for aid in self.agent_ids}
            
            # Display events grouped by iteration
            for iteration in sorted(events_by_iteration.keys()):
                # Get round number from the first event in this iteration
                current_round = 0
                if events_by_iteration[iteration]:
                    first_event = events_by_iteration[iteration][0]
                    current_round = first_event.context.get("round", 0) if first_event.context else 0
                
                if iteration == 0:
                    f.write("INITIALIZATION:\n")
                else:
                    f.write(f"ITERATION {iteration} (Round {current_round}):\n")
                f.write("  " + "-" * 40 + "\n")
                
                events = events_by_iteration[iteration]
                
                # Process events and update agent statuses
                for event in events:
                    agent_display = self._get_agent_display_name(event.agent_id) if event.agent_id else "System"
                    timestamp_str = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S.%f")[:-3]
                    
                    # Update tracked status if this is a status change event
                    if event.event_type == "status_change" and event.agent_id:
                        # Extract new status from event details using enum values
                        enum_statuses = [status.value.upper() for status in AgentStatus]
                        custom_statuses = ["IDLE", "WORKING"]  # Non-enum statuses
                        all_statuses = enum_statuses + custom_statuses
                        
                        for status in all_statuses:
                            if status.lower() in event.details.lower():
                                agent_current_status[event.agent_id] = status
                                break
                    
                    f.write(f"  [{timestamp_str}] {agent_display}: ")
                    
                    if event.event_type == "iteration_start":
                        f.write(f"--- Starting coordination iteration ---\n")
                    elif event.event_type == "new_answer":
                        label = event.context.get("label", "unknown") if event.context else "unknown"
                        f.write(f"PROVIDED ANSWER ({label})\n")
                    elif event.event_type == "vote_cast":
                        if event.context:
                            voted_for = event.context.get("agent_id", "unknown")
                            reason = event.context.get("reason", "No reason")
                            f.write(f"VOTED FOR {voted_for} - {reason}\n")
                        else:
                            f.write(f"VOTED (details unavailable)\n")
                    elif event.event_type == "restart_triggered":
                        if event.context:
                            affected = event.context.get("affected_agents", [])
                            new_round = event.context.get("new_round", "?")
                            f.write(f"TRIGGERED RESTART - Affected: {len(affected)} agents, Starting Round {new_round}\n")
                        else:
                            f.write(f"TRIGGERED RESTART\n")
                    elif event.event_type == "status_change":
                        f.write(f"{event.details}\n")
                    elif event.event_type == "final_agent_selected":
                        f.write(f"SELECTED AS FINAL PRESENTER\n")
                    elif event.event_type == "final_answer":
                        label = event.context.get("label", "unknown") if event.context else "unknown"
                        f.write(f"PRESENTED FINAL ANSWER ({label})\n")
                    elif event.event_type == "agent_error":
                        f.write(f"ERROR: {event.details}\n")
                    elif event.event_type == "agent_timeout":
                        f.write(f"TIMEOUT: {event.details}\n")
                    elif event.event_type == "agent_cancelled":
                        f.write(f"CANCELLED: {event.details}\n")
                    elif event.event_type == "agent_vote_ignored":
                        f.write(f"VOTE IGNORED: {event.details}\n")
                    elif event.event_type == "agent_restart":
                        f.write(f"RESTARTING: {event.details}\n")
                    else:
                        f.write(f"{event.event_type}: {event.details}\n")
                
                # Show status summary at end of iteration
                f.write("\n  STATUS AT END OF ITERATION:\n")
                for aid in self.agent_ids:
                    agent_display = self._get_agent_display_name(aid)
                    status = agent_current_status.get(aid, "UNKNOWN")
                    f.write(f"    {agent_display}: {status}\n")
                        
                f.write("\n")
            
            f.write("\n")
            
            # Agent-grouped summary
            f.write("AGENT ACTION SUMMARY:\n")
            f.write("=" * 50 + "\n")
            
            for agent_id in self.agent_ids:
                agent_display = self._get_agent_display_name(agent_id)
                f.write(f"\n{agent_display} ({agent_id}):\n")
                f.write("-" * 30 + "\n")
                
                agent_events = [e for e in sorted_events if e.agent_id == agent_id]
                if not agent_events:
                    f.write("  (No recorded actions)\n")
                    continue
                
                for event in agent_events:
                    timestamp_str = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
                    
                    if event.event_type == "new_answer":
                        label = event.context.get("label", "unknown") if event.context else "unknown"
                        f.write(f"  [{timestamp_str}] Provided answer: {label}\n")
                        
                    elif event.event_type == "vote_cast":
                        if event.context:
                            voted_for = event.context.get("voted_for", "unknown")
                            reason = event.context.get("reason", "No reason")
                            f.write(f"  [{timestamp_str}] Voted for: {voted_for} ({reason})\n")
                        
                    elif event.event_type == "restart_triggered":
                        f.write(f"  [{timestamp_str}] Triggered coordination restart\n")
                        
                    elif event.event_type == "status_change":
                        f.write(f"  [{timestamp_str}] Status: {event.details}\n")
                        
                    elif event.event_type == "final_answer":
                        label = event.context.get("label", "unknown") if event.context else "unknown"
                        f.write(f"  [{timestamp_str}] Presented final answer: {label}\n")
            
            f.write("\n" + "=" * 100 + "\n")
    
    def _create_round_timeline_file(self, log_dir):
        """Create a round-focused timeline with table display (new format)."""
        round_file = log_dir / "coordination_rounds.txt"
        
        with open(round_file, 'w', encoding='utf-8') as f:
            # Header
            f.write("=" * 120 + "\n")
            f.write("MASSGEN COORDINATION ROUNDS - TABLE VIEW\n")
            f.write("=" * 120 + "\n\n")
            
            # Description
            f.write("DESCRIPTION:\n")
            f.write("This shows coordination organized by rounds in a table format similar to coordination_timeline.txt.\n")
            f.write("A new round starts when any agent provides a new answer.\n\n")
            
            # Agent mapping
            f.write("AGENT MAPPING:\n")
            for i, agent_id in enumerate(self.agent_ids):
                f.write(f"  Agent{i+1} = {agent_id}\n")
            f.write("\n")
            
            # Summary stats
            summary = self.get_summary()
            f.write(f"Duration: {summary['duration']:.1f}s | ")
            f.write(f"Rounds: {self.get_max_round() + 1} | ")
            f.write(f"Winner: {summary['final_winner'] or 'None'}\n\n")
            
            # Generate rounds for table display
            rounds_data = self._generate_rounds_for_table()
            
            # Write the table (with initial user prompt)
            self._write_rounds_table(f, rounds_data)
            
            f.write("\nKEY CONCEPTS:\n")
            f.write("-" * 20 + "\n")
            f.write("• NEW ANSWER: Agent provides original response\n")
            f.write("• RESTART: Other agents discard work, restart with new context\n")
            f.write("• VOTING: Agents choose best answer from available options\n")
            f.write("• FINAL: Winning agent presents synthesized response\n")
            f.write("• ERROR/TIMEOUT: Agent encounters issues\n\n")
            f.write("=" * 120 + "\n")
    
    def _generate_rounds_for_table(self) -> List[Dict[str, Any]]:
        """Generate round steps from events for table visualization.
        
        Instead of grouping by round numbers (which can be confusing with per-agent rounds),
        create logical phases based on the chronological flow of events.
        """
        rounds = []
        
        # Sort events by timestamp
        sorted_events = sorted(self.events, key=lambda e: e.timestamp)
        
        # Create logical phases based on event sequence
        phases = []
        current_phase = {"events": [], "type": "answering", "round_num": 0}
        
        # Find key transition points to determine phases
        for event in sorted_events:
            if event.event_type in ["session_start", "session_end"]:
                continue
                
            if event.event_type == "new_answer":
                # Each new answer potentially starts a new phase
                if current_phase["events"] and any(e.event_type == "new_answer" for e in current_phase["events"]):
                    # Previous phase already had an answer, start new phase
                    phases.append(current_phase)
                    current_phase = {"events": [event], "type": "answering", "round_num": len(phases)}
                else:
                    # Add to current answering phase
                    current_phase["events"].append(event)
                    
            elif event.event_type == "vote_cast":
                # Voting events go in a separate voting phase
                if current_phase["type"] != "voting":
                    # Start new voting phase
                    if current_phase["events"]:
                        phases.append(current_phase)
                    current_phase = {"events": [event], "type": "voting", "round_num": len(phases)}
                else:
                    # Add to current voting phase
                    current_phase["events"].append(event)
                    
            elif event.event_type in ["final_round_start", "final_answer"]:
                # Final events go in final phase
                if current_phase["type"] != "final":
                    if current_phase["events"]:
                        phases.append(current_phase)
                    current_phase = {"events": [event], "type": "final", "round_num": len(phases)}
                else:
                    current_phase["events"].append(event)
            else:
                # Other events (context_received, status_change, etc.) join current phase
                current_phase["events"].append(event)
        
        # Add the last phase
        if current_phase["events"]:
            phases.append(current_phase)
        
        # Track context history across rounds for each agent
        agent_context_history = {aid: [] for aid in self.agent_ids}
        
        # Convert phases to display rounds  
        for phase in phases:
            round_num = phase["round_num"]
            events = phase["events"]
            phase_type = phase["type"]
            
            # Initialize round data
            round_data = {
                "round": round_num,
                "events": events,  # Store the events for this round
                "agents": {aid: {"status": AgentStatus.ANSWERING.value.upper(), "context": [], "details": ""} for aid in self.agent_ids},
                "is_final": (phase_type == "final"),
                "phase_type": phase_type
            }
            
            # Process events in this round
            main_event = None
            main_event_type = ""
            
            # First pass: Find all event types and context updates
            new_answer_events = []  # Collect ALL new_answer events, not just the last one
            vote_events = []
            restart_event = None
            final_events = []
            context_events = {}  # agent_id -> context_received event
            
            for event in events:
                if event.event_type == "new_answer":
                    new_answer_events.append(event)  # Collect all answers, don't overwrite
                elif event.event_type == "vote_cast":
                    vote_events.append(event)
                elif event.event_type == "restart_triggered":
                    restart_event = event
                elif event.event_type in ["final_round_start", "final_answer"]:
                    final_events.append(event)
                elif event.event_type == "context_received":
                    # Track what context each agent received in this round
                    context_events[event.agent_id] = event
            
            # Update agent contexts based on context_received events
            # But ONLY if the agent is NOT providing a new answer in this same round
            # (since context should show what they had BEFORE producing the answer)
            for aid, ctx_event in context_events.items():
                if ctx_event.context:
                    # Check if this agent is also providing an answer in the same round
                    agent_answering_in_round = any(event.agent_id == aid for event in new_answer_events)
                    
                    if not agent_answering_in_round:
                        # Agent is not answering in this round, so show their current context
                        if "available_answer_labels" in ctx_event.context:
                            new_context = ctx_event.context["available_answer_labels"]
                            round_data["agents"][aid]["context"] = new_context
                            # Update context history for future rounds
                            agent_context_history[aid] = new_context.copy()
                        elif "available_answers" in ctx_event.context:
                            # Fallback for older events that don't have labels
                            new_context = ctx_event.context["available_answers"]
                            round_data["agents"][aid]["context"] = new_context
                            agent_context_history[aid] = new_context.copy()
                    else:
                        # Agent IS answering in this round, show context they had BEFORE this round
                        round_data["agents"][aid]["context"] = agent_context_history[aid].copy()
                        
                        # Update their context history with new context for next round
                        if "available_answer_labels" in ctx_event.context:
                            agent_context_history[aid] = ctx_event.context["available_answer_labels"].copy()
                        elif "available_answers" in ctx_event.context:
                            agent_context_history[aid] = ctx_event.context["available_answers"].copy()
            
            # For agents without context_received events in this round, use their context history
            for aid in self.agent_ids:
                if aid not in context_events and aid in round_data["agents"]:
                    round_data["agents"][aid]["context"] = agent_context_history[aid].copy()
            
            # Determine the main event for this round
            if new_answer_events:
                # This round shows new answers - handle multiple answers in same round
                if len(new_answer_events) == 1:
                    # Single answer in this round
                    answer_event = new_answer_events[0]
                    label = answer_event.context.get("label", "unknown") if answer_event.context else "unknown"
                    main_event_type = f"NEW ANSWER: {label}"
                    main_event = answer_event
                    
                    # Update answering agent
                    existing_context = round_data["agents"][answer_event.agent_id].get("context", [])
                    round_data["agents"][answer_event.agent_id] = {
                        "status": f"NEW ANSWER: {label}",
                        "context": existing_context,
                        "details": f"Provided answer {label}"
                    }
                    
                    # Update other agents status
                    for aid in self.agent_ids:
                        if aid != answer_event.agent_id:
                            round_data["agents"][aid]["details"] = "Waiting for coordination to continue..."
                else:
                    # Multiple answers in this round - show all of them
                    answer_labels = []
                    for answer_event in new_answer_events:
                        label = answer_event.context.get("label", "unknown") if answer_event.context else "unknown"
                        answer_labels.append(label)
                        
                        # Update each answering agent
                        existing_context = round_data["agents"][answer_event.agent_id].get("context", [])
                        round_data["agents"][answer_event.agent_id] = {
                            "status": f"NEW ANSWER: {label}",
                            "context": existing_context,
                            "details": f"Provided answer {label}"
                        }
                    
                    main_event_type = f"MULTIPLE ANSWERS: {', '.join(answer_labels)}"
                    main_event = new_answer_events[0]  # Use first for reference
                
                # IMPORTANT: Check if this answer also triggered a restart in the same round
                # if restart_event and restart_event.agent_id == new_answer_event.agent_id:
                #     # This agent provided an answer AND triggered a restart
                #     affected_raw = restart_event.context.get("affected_agents", []) if restart_event.context else []
                    
                #     # Handle both list and string representations of affected_agents
                #     if isinstance(affected_raw, str) and "dict_keys" in affected_raw:
                #         import re
                #         matches = re.findall(r"'([^']+)'", affected_raw)
                #         affected = matches
                #     elif isinstance(affected_raw, list):
                #         affected = affected_raw
                #     else:
                #         affected = []
                    
                #     # Update answering agent to show they triggered restart
                #     round_data["agents"][new_answer_event.agent_id]["details"] = f"Provided {label} → triggered restart"
                    
                #     # Update other affected agents to show restart
                #     for aid in affected:
                #         if aid != new_answer_event.agent_id:
                #             round_data["agents"][aid] = {
                #                 "status": AgentStatus.RESTARTING.value.upper(),
                #                 "context": global_agent_context[aid].copy(),
                #                 "details": "Previous work discarded, restarting with new context"
                #             }
            
            elif vote_events:
                # This is a voting round
                main_event_type = "VOTING PHASE (system-wide)"
                main_event = vote_events[0]
                
                # Update all voting agents
                for vote_event in vote_events:
                    if vote_event.context:
                        voted_for_label = vote_event.context.get("voted_for_label", vote_event.context.get("voted_for", "unknown"))
                        # Keep existing context from context_received event
                        existing_context = round_data["agents"][vote_event.agent_id].get("context", [])
                        round_data["agents"][vote_event.agent_id] = {
                            "status": f"VOTE: {voted_for_label}",
                            "context": existing_context,  # Use context from context_received event
                            "details": f"Selected: {voted_for_label}"
                        }
            
            elif final_events:
                # This is the final round
                final_answer_event = None
                for event in final_events:
                    if event.event_type == "final_answer":
                        final_answer_event = event
                        break
                
                if final_answer_event:
                    label = final_answer_event.context.get("label", "unknown") if final_answer_event.context else "unknown"
                    main_event_type = f"FINAL ANSWER: {label}"
                    main_event = final_answer_event
                    
                    # Update final agent
                    # Keep existing context from context_received event
                    existing_context = round_data["agents"][final_answer_event.agent_id].get("context", [])
                    round_data["agents"][final_answer_event.agent_id] = {
                        "status": f"FINAL ANSWER: {label}",
                        "context": existing_context,  # Use context from context_received event
                        "details": "Presented final answer"
                    }
                    
                    # Mark other agents as terminated
                    for aid in self.agent_ids:
                        if aid != final_answer_event.agent_id:
                            round_data["agents"][aid]["status"] = "TERMINATED"
                else:
                    main_event_type = "FINAL ROUND START"
                    main_event = final_events[0]
                    round_data["is_final"] = True
            
            elif restart_event:
                # Show restart as a separate event
                affected_raw = restart_event.context.get("affected_agents", []) if restart_event.context else []
                
                # Handle both list and string representations of affected_agents
                if isinstance(affected_raw, str) and "dict_keys" in affected_raw:
                    # Parse string like "dict_keys(['gpt5nano_1', 'gpt5nano_2'])"
                    import re
                    matches = re.findall(r"'([^']+)'", affected_raw)
                    affected = matches
                elif isinstance(affected_raw, list):
                    affected = affected_raw
                else:
                    affected = []
                
                triggering_agent = self._get_agent_display_name(restart_event.agent_id)
                main_event_type = f"RESTART triggered by {triggering_agent}"
                main_event = restart_event
                
                # Update triggering agent (keeps their answer status)
                round_data["agents"][restart_event.agent_id]["details"] = f"Triggered restart"
                
                # Update all affected agents (not including the triggering agent)
                for aid in affected:
                    if aid != restart_event.agent_id:  # Don't override triggering agent
                        # Keep existing context from context_received event
                        existing_context = round_data["agents"][aid].get("context", [])
                        round_data["agents"][aid] = {
                            "status": AgentStatus.RESTARTING.value.upper(),
                            "context": existing_context,  # Use context from context_received event
                            "details": "Previous work discarded, restarting with new context"
                        }
            
            # Add the round
            round_data["event"] = main_event_type
            rounds.append(round_data)
        
        return rounds
    
    def _write_rounds_table(self, f, rounds_data):
        """Write the rounds table to file."""
        if not rounds_data:
            return
        
        # Calculate column widths (no events column, wider agent columns)
        col_widths = {
            "round": 8,
            "agent": 60  # wider columns for more detailed info
        }
        
        # Table header
        total_width = col_widths["round"] + len(self.agent_ids) * col_widths["agent"] + len(self.agent_ids) + 2
        f.write("+" + "-" * (total_width - 1) + "+\n")
        
        header = f"| {'Round':^{col_widths['round']}} |"
        for i in range(len(self.agent_ids)):
            header += f" {'Agent' + str(i+1):^{col_widths['agent']}} |"
        f.write(header + "\n")
        
        # Separator
        f.write("|" + "-" * (col_widths["round"] + 2) + "+")
        for _ in self.agent_ids:
            f.write("-" * (col_widths["agent"] + 2) + "+")
        f.write("\n")
        
        # Initial user prompt row (spans all columns)
        if self.user_prompt:
            prompt_preview = self.format_answer_preview(self.user_prompt, max_length=200)
            total_content_width = len(self.agent_ids) * (col_widths["agent"] + 3) - 1  # Account for separators
            
            f.write(f"| {'USER':^{col_widths['round']}} | {prompt_preview:^{total_content_width}} |\n")
            
            # Separator after user prompt
            f.write("|" + "=" * (col_widths["round"] + 2) + "+")
            for _ in self.agent_ids:
                f.write("=" * (col_widths["agent"] + 2) + "+")
            f.write("\n")
        
        # Table rows
        for round_data in rounds_data:
            round_label = f"R{round_data['round']}" if not round_data["is_final"] else "FINAL"
            
            # Create comprehensive cell content for each agent
            agent_cells = []
            for aid in self.agent_ids:
                agent_info = round_data["agents"].get(aid, {})
                cell_lines = []
                
                # Get status and context first
                status = agent_info.get("status", "IDLE")
                context = agent_info.get("context", [])
                
                # Format status: keep special statuses as-is, but make basic statuses lowercase with parentheses
                basic_statuses = [
                    AgentStatus.ANSWERING.value.upper(),
                    AgentStatus.VOTING.value.upper(), 
                    AgentStatus.RESTARTING.value.upper(),
                    AgentStatus.ERROR.value.upper(),
                    AgentStatus.TIMEOUT.value.upper(),
                    AgentStatus.COMPLETED.value.upper(),
                    "IDLE", "TERMINATED"  # Custom statuses not in enum
                ]
                if status in basic_statuses:
                    formatted_status = f"({status.lower()})"
                else:
                    # Keep special statuses like "NEW ANSWER: agent1.1" as-is
                    formatted_status = status
                
                # Add context FIRST (since it leads to the action)
                if context:
                    context_str = ", ".join(context)
                    context_str = self.format_answer_preview(context_str, max_length=45)
                    cell_lines.append(f"Context: [{context_str}]")
                else:
                    cell_lines.append("Context: []")
                
                # Then add the status/action
                cell_lines.append(formatted_status)
                
                # Add specific information based on status
                if "NEW ANSWER:" in status:
                    # Agent is providing an answer - add answer preview
                    import re
                    match = re.search(r'(agent\d+\.\d+|agent\d+\.final)', status)
                    if match:
                        label = match.group(1)
                        answer_content = self.all_answers.get(label, "")
                        if answer_content:
                            preview = self.format_answer_preview(answer_content, max_length=45)
                            cell_lines.append(f"Preview: {preview}")
                
                elif "VOTE:" in status:
                    # Agent is voting - add vote reason and options
                    vote_reason = ""
                    vote_options = []
                    for vote in self.votes:
                        if vote.voter_id == aid and vote.available_answers:
                            vote_options = vote.available_answers
                            vote_reason = vote.reason or ""
                            break
                    
                    if vote_reason:
                        vote_reason = self.format_answer_preview(vote_reason, max_length=45)
                        cell_lines.append(f"Reason: {vote_reason}")
                    
                    if vote_options:
                        options_str = ", ".join(vote_options)
                        options_str = self.format_answer_preview(options_str, max_length=45)
                        cell_lines.append(f"Options: [{options_str}]")
                
                elif "FINAL ANSWER:" in status:
                    # Agent is presenting final answer - add final answer preview
                    import re
                    match = re.search(r'(agent\d+\.\d+|agent\d+\.final)', status)
                    if match:
                        label = match.group(1)
                        answer_content = self.all_answers.get(label, "")
                        if answer_content:
                            preview = self.format_answer_preview(answer_content, max_length=45)
                            cell_lines.append(f"Preview: {preview}")
                
                elif status == AgentStatus.RESTARTING.value.upper() or formatted_status == f"({AgentStatus.RESTARTING.value})":
                    # Agent is restarting - add restart info
                    cell_lines.append("Restarting with new context")
                
                elif status == "TERMINATED":
                    # Agent is done - no additional info needed
                    pass
                
                agent_cells.append(cell_lines)
            
            # Calculate number of rows needed
            max_lines = max(len(cell) for cell in agent_cells) if agent_cells else 1
            
            # Check if this round contains actual restart events
            restart_affected_agents = set()
            
            # Look for restart_triggered events in this round's events
            for event in round_data.get("events", []):
                if event.event_type == "restart_triggered":
                    # Found a restart event - extract affected agents
                    if event.context:
                        affected_raw = event.context.get("affected_agents", [])
                        
                        # Handle both list and string representations of affected_agents
                        if isinstance(affected_raw, str) and "dict_keys" in affected_raw:
                            # Parse string like "dict_keys(['gpt5nano_1', 'gpt5nano_2'])"
                            import re
                            matches = re.findall(r"'([^']+)'", affected_raw)
                            affected = matches
                        elif isinstance(affected_raw, list):
                            affected = affected_raw
                        else:
                            affected = []
                        
                        # Add affected agents to the restart set
                        for aid in affected:
                            if aid in self.agent_ids:
                                restart_affected_agents.add(aid)
                    break  # Only need to find one restart event per round
            
            # Write multi-row cell content
            for line_num in range(max_lines):
                if line_num == 0:
                    # First line includes round label
                    row = f"| {round_label:^{col_widths['round']}} |"
                else:
                    # Subsequent lines have empty round column
                    row = f"| {' ':^{col_widths['round']}} |"
                
                for i, (aid, cell_lines) in enumerate(zip(self.agent_ids, agent_cells)):
                    if line_num < len(cell_lines):
                        content = cell_lines[line_num][:col_widths["agent"]]
                        # Center the content
                        row += f" {content:^{col_widths['agent']}} |"
                    elif line_num == 0 and aid in restart_affected_agents and len(restart_affected_agents) > 1:
                        # Show restart indicator across affected agents
                        restart_indicator = "↻ RESTART ↻"
                        row += f" {restart_indicator:^{col_widths['agent']}} |"
                    else:
                        row += f" {' ':^{col_widths['agent']}} |"
                
                f.write(row + "\n")
            
            # Check if this was a restart round and add special separator
            if len(restart_affected_agents) > 0:
                # Special restart separator with visual indicators
                f.write("|" + "~" * (col_widths["round"] + 2) + "+")
                for aid in self.agent_ids:
                    if aid in restart_affected_agents:
                        # Use wave pattern for restarted agents
                        f.write("~" * (col_widths["agent"] + 2) + "+")
                    else:
                        # Normal separator for non-affected agents
                        f.write("-" * (col_widths["agent"] + 2) + "+")
                f.write("\n")
            else:
                # Normal row separator
                f.write("|" + "-" * (col_widths["round"] + 2) + "+")
                for _ in self.agent_ids:
                    f.write("-" * (col_widths["agent"] + 2) + "+")
                f.write("\n")
        
        # Table footer
        f.write("+" + "-" * (total_width - 1) + "+")

    def _get_agent_display_name(self, agent_id: str) -> str:
        """Get display name for agent (Agent1, Agent2, etc.)."""
        if agent_id in self.agent_ids:
            return f"Agent{self.agent_ids.index(agent_id) + 1}"
        return agent_id
    
    def _get_anonymous_agent_id(self, agent_id: str) -> str:
        """Get anonymous agent ID (agent1, agent2, etc.) for an agent."""
        if agent_id in self.agent_ids:
            return f"agent{self.agent_ids.index(agent_id) + 1}"
        return "unknown"
    
    def _write_timeline_table(self, f, steps):
        """Write the timeline table to file."""
        # Calculate column widths
        col_widths = {
            "step": 8,
            "event": 35,
            "agent": 40  # per agent
        }
        
        # Table header
        f.write("+" + "-" * (col_widths["step"] + col_widths["event"] + len(self.agent_ids) * col_widths["agent"] + len(self.agent_ids) + 2) + "+\n")
        
        header = f"| {'Step':^{col_widths['step']}} | {'Event':^{col_widths['event']}} |"
        for i in range(len(self.agent_ids)):
            header += f" {'Agent' + str(i+1):^{col_widths['agent']}} |"
        f.write(header + "\n")
        
        # Separator
        f.write("|" + "-" * (col_widths["step"] + 2) + "+" + "-" * (col_widths["event"] + 2) + "+")
        for _ in self.agent_ids:
            f.write("-" * (col_widths["agent"] + 2) + "+")
        f.write("\n")
        
        # Table rows
        for step in steps:
            # Main status row
            row = f"| {step['step']:^{col_widths['step']}} | {step['event'][:col_widths['event']]:^{col_widths['event']}} |"
            
            for aid in self.agent_ids:
                agent_info = step["agents"].get(aid, {})
                status = agent_info.get("status", "")[:col_widths["agent"]]
                row += f" {status:^{col_widths['agent']}} |"
            f.write(row + "\n")
            
            # Context and details rows
            has_content = False
            for aid in self.agent_ids:
                agent_info = step["agents"].get(aid, {})
                if agent_info.get("context") or agent_info.get("details"):
                    has_content = True
                    break
            
            if has_content:
                # Context row
                row = f"| {' ':^{col_widths['step']}} | {' ':^{col_widths['event']}} |"
                for aid in self.agent_ids:
                    agent_info = step["agents"].get(aid, {})
                    context = agent_info.get("context", [])
                    if context:
                        context_str = f"Context: {context}"[:col_widths["agent"]]
                    else:
                        context_str = ""
                    row += f" {context_str:^{col_widths['agent']}} |"
                f.write(row + "\n")
                
                # Details row
                row = f"| {' ':^{col_widths['step']}} | {' ':^{col_widths['event']}} |"
                for aid in self.agent_ids:
                    agent_info = step["agents"].get(aid, {})
                    details = agent_info.get("details", "")
                    if details:
                        # Take first line of details
                        details_line = details.split('\n')[0][:col_widths["agent"]]
                    else:
                        details_line = ""
                    row += f" {details_line:^{col_widths['agent']}} |"
                f.write(row + "\n")
            
            # Row separator
            f.write("|" + "-" * (col_widths["step"] + 2) + "+" + "-" * (col_widths["event"] + 2) + "+")
            for _ in self.agent_ids:
                f.write("-" * (col_widths["agent"] + 2) + "+")
            f.write("\n")
        
        # Table footer
        f.write("+" + "-" * (col_widths["step"] + col_widths["event"] + len(self.agent_ids) * col_widths["agent"] + len(self.agent_ids) + 2) + "+\n")
    
    def get_rich_timeline_content(self):
        """Get timeline content for Rich terminal display integration."""
        try:
            timeline_steps = self._generate_timeline_steps()
            summary = self.get_summary()
            
            # Convert to display format with previews
            display_answers = self.get_answers_for_display()
            
            # Add preview to timeline steps
            for step in timeline_steps:
                for agent_id in step["agents"]:
                    agent_info = step["agents"][agent_id]
                    # Add preview if this is an answer step
                    if "NEW:" in agent_info.get("status", "") or "FINAL:" in agent_info.get("status", ""):
                        # Extract label from status
                        import re
                        match = re.search(r'(agent\d+\.\d+|agent\d+\.final)', agent_info["status"])
                        if match:
                            label = match.group(1)
                            if label in display_answers:
                                agent_info["preview"] = f"Preview: {display_answers[label]['preview']}"
            
            return {
                'agent_ids': self.agent_ids,
                'summary': summary,
                'timeline_steps': timeline_steps
            }
        except Exception as e:
            print(f"Failed to generate rich timeline content: {e}")
            return None
