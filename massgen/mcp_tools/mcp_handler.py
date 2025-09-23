"""
MCP Handler class for consolidating common MCP logic across backends.
This class encapsulates all common MCP operations and provides a delegate pattern
for backend-specific implementations.
"""

from __future__ import annotations
import asyncio
import inspect
import json
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional
from ..backend.base import StreamChunk
from ..logger_config import logger, log_mcp_activity
from .backend_utils import (
    Function,
    MCPConfigHelper,
    MCPCircuitBreakerManager,
    MCPErrorHandler,
    MCPMessageManager,
    MCPResourceManager,
    MCPSetupManager,
    MultiMCPClient,
    MCPExecutionManager,
)
from .circuit_breaker import MCPCircuitBreaker
from .backend_utils import MCPState


class MCPHandler:
    """
    Consolidated MCP logic handler for backend implementations.

    This class encapsulates common MCP operations and provides a delegate pattern
    for backend-specific implementations like parameter building, streaming, and
    message formatting.
    """

    def __init__(self, backend) -> None:
        """
        Initialize MCP handler with backend reference.

        Args:
            backend: The backend instance this handler is attached to
        """
        self.backend = backend
        self.logger = logger
        self._execution_manager = MCPExecutionManager()
        self._last_mcp_servers_used = []
        self._servers_signature = None
        # Initialize state if not already set
        if not hasattr(self.backend, "_mcp_state"):
            setattr(self.backend, "_mcp_state", MCPState.NOT_INITIALIZED)

    def should_use_mcp(self) -> bool:
        """Determine if MCP should be used based on configuration, setup status, and circuit breaker state."""
        mcp_servers = getattr(self.backend, "mcp_servers", [])
        functions = self._get_functions()

        # Basic check: need both servers configured and functions available
        if not (functions and mcp_servers):
            return False

        # Check MCP state - only use if READY
        mcp_state = getattr(self.backend, "_mcp_state", MCPState.NOT_INITIALIZED)
        if mcp_state != MCPState.READY:
            return False

        return True

    def should_show_no_mcp_message(self) -> bool:
        """Determine if 'no MCP mode' message should be shown."""
        mcp_servers = getattr(self.backend, "mcp_servers", [])
        functions = self._get_functions()

        # Must have servers configured to show "no MCP mode" message
        if not mcp_servers:
            return False

        # Check if MCP is permanently blocked by circuit breaker
        if getattr(self.backend, "_mcp_permanently_blocked", False):
            # Only show message once when permanently blocked
            if not getattr(self.backend, "_mcp_permanent_block_notified", False):
                setattr(self.backend, "_mcp_permanent_block_notified", True)
                return True
            return False
            
        # Don't show if setup failed message was already shown during setup attempts
        if getattr(self.backend, "_mcp_connection_failure_notified", False):
            return False
            
        # Check if circuit breaker is blocking (warning already shown)
        circuit_breaker = self._get_circuit_breaker()
        circuit_breakers_enabled = getattr(self.backend, "_circuit_breakers_enabled", False)
        
        if circuit_breakers_enabled and circuit_breaker:
            # If circuit breaker has already shown blocking message, don't show setup failed
            if getattr(self.backend, "_mcp_circuit_breaker_notified", False):
                return False

        # Setup failed (no functions available) - only show if not due to circuit breaker blocking
        if not functions:
            return True

        return False

    async def setup_mcp_tools(self) -> None:
        """
        Initialize MCP client and convert tools to Function objects.

        This method handles:
        - Server normalization and filtering
        - MCP client setup with circuit breaker retry coordination
        - Tool conversion to Function objects
        - Circuit breaker state management
        """
        mcp_servers = getattr(self.backend, "mcp_servers", [])
        if not mcp_servers:
            return

        # Check if already initialized successfully
        current_state = getattr(self.backend, "_mcp_state", MCPState.NOT_INITIALIZED)
        if current_state == MCPState.READY:
            return
            
        # Check if setup is permanently blocked by circuit breaker
        if getattr(self.backend, "_mcp_permanently_blocked", False):
            return

        try:
            # Normalize and separate MCP servers by transport type using mcp_tools utilities
            normalized_servers = MCPSetupManager.normalize_mcp_servers(
                mcp_servers,
                backend_name=self._get_backend_name(),
                agent_id=self._get_agent_id(),
            )

            new_signature = tuple(
                sorted([s.get("name", "") for s in normalized_servers])
            )
            if (
                self._servers_signature == new_signature
                and current_state == MCPState.READY
            ):
                return

            # If the signature has changed, we need to re-initialize the client and function registry.
            if self._servers_signature != new_signature:
                self.logger.info(
                    f"MCP server signature changed. Re-initializing MCP client."
                )
                self._servers_signature = new_signature
                # Reset state on server configuration change
                setattr(self.backend, "_mcp_state", MCPState.NOT_INITIALIZED)
                if self._get_mcp_client():
                    await self.cleanup_mcp()

            mcp_tools_servers = MCPSetupManager.separate_stdio_streamable_servers(
                normalized_servers,
                backend_name=self._get_backend_name(),
                agent_id=self._get_agent_id(),
            )

            if not mcp_tools_servers:
                logger.info("No stdio/streamable-http servers configured")
                return

            # Setup with circuit breaker retry coordination
            circuit_breaker = self._get_circuit_breaker()
            _circuit_breakers_enabled = getattr(
                self.backend, "_circuit_breakers_enabled", False
            )
            
            if _circuit_breakers_enabled and circuit_breaker:
                # Attempt setup with circuit breaker coordination up to max_failures times
                max_attempts = circuit_breaker.config.max_failures
                
                for attempt in range(1, max_attempts + 1):
                    self.logger.info(f"MCP setup attempt {attempt}/{max_attempts}")
                    
                    # Check circuit breaker filtering
                    filtered_servers = (
                        MCPCircuitBreakerManager.apply_circuit_breaker_filtering(
                            mcp_tools_servers,
                            circuit_breaker,
                            backend_name=self._get_backend_name(),
                            agent_id=self._get_agent_id(),
                        )
                    )
                    
                    if not filtered_servers:
                        # All servers blocked by circuit breaker
                        if not getattr(self.backend, "_mcp_circuit_breaker_notified", False):
                            logger.warning(
                                "All MCP servers blocked by circuit breaker during setup"
                            )
                            setattr(self.backend, "_mcp_circuit_breaker_notified", True)
                        
                        # Mark as permanently blocked after max attempts
                        if attempt >= max_attempts:
                            setattr(self.backend, "_mcp_permanently_blocked", True)
                        return
                    
                    # Attempt connection
                    success = await self._attempt_mcp_setup(
                        filtered_servers, circuit_breaker, mcp_tools_servers
                    )
                    
                    if success:
                        self.logger.info(f"MCP setup successful on attempt {attempt}")
                        return
                    
                    # If this was the last attempt, mark as permanently blocked
                    if attempt >= max_attempts:
                        self.logger.warning(f"MCP setup failed after {max_attempts} attempts - permanently blocked")
                        setattr(self.backend, "_mcp_permanently_blocked", True)
                        return
                    
                    # Wait before next attempt
                    await asyncio.sleep(0.2)
            else:
                # No circuit breaker - single attempt
                await self._attempt_mcp_setup(mcp_tools_servers, None, mcp_tools_servers)
                
        except Exception as e:
            # Record failure for circuit breaker
            circuit_breaker = self._get_circuit_breaker()
            _circuit_breakers_enabled = getattr(
                self.backend, "_circuit_breakers_enabled", False
            )

            if _circuit_breakers_enabled and circuit_breaker:
                try:
                    servers_to_use = self._last_mcp_servers_used or locals().get(
                        "mcp_tools_servers", []
                    )
                    await MCPCircuitBreakerManager.record_failure(
                        servers_to_use,
                        circuit_breaker,
                        str(e),
                        backend_name=self._get_backend_name(),
                        agent_id=self._get_agent_id(),
                        backend_instance=self.backend,
                    )
                except Exception as cb_error:
                    logger.warning(
                        f"Failed to record circuit breaker failure: {cb_error}"
                    )

            logger.warning(f"Failed to setup MCP sessions: {e}")
            self._set_mcp_client(None)
            setattr(self.backend, "_mcp_state", MCPState.NOT_INITIALIZED)
            self._set_functions({})
            # Don't re-raise the exception - let the backend handle fallback gracefully
            
    async def _attempt_mcp_setup(
        self, servers_to_use: list, circuit_breaker, original_servers: list
    ) -> bool:
        """Attempt MCP setup with given servers. Returns True if successful."""
        try:
            allowed_tools = getattr(self.backend, "allowed_tools", None)
            exclude_tools = getattr(self.backend, "exclude_tools", None)
            
            self._last_mcp_servers_used = servers_to_use

            # Setup MCP client using consolidated utilities
            mcp_client = await MCPResourceManager.setup_mcp_client(
                servers=servers_to_use,
                allowed_tools=allowed_tools,
                exclude_tools=exclude_tools,
                circuit_breaker=circuit_breaker,
                timeout_seconds=self.backend.config.get("mcp_timeout_seconds", 30),
                backend_name=self._get_backend_name(),
                agent_id=self._get_agent_id(),
            )
            
            # Guard after client setup
            if not mcp_client:
                # Let circuit breaker handle retry logic - don't set permanent failure state
                # Only show warning once per session
                if not getattr(self.backend, "_mcp_connection_failure_notified", False):
                    logger.warning(
                        "MCP client setup failed, falling back to no-MCP streaming"
                    )
                    setattr(self.backend, "_mcp_connection_failure_notified", True)
                return False
                
            # Convert tools to functions using consolidated utility
            functions = self._get_functions()
            functions.update(
                MCPResourceManager.convert_tools_to_functions(
                    mcp_client,
                    backend_name=self._get_backend_name(),
                    agent_id=self._get_agent_id(),
                )
            )
            self._set_functions(functions)
            self._set_mcp_client(mcp_client)
            setattr(self.backend, "_mcp_state", MCPState.READY)

            logger.info(
                f"Successfully initialized MCP mcp_tools sessions with {len(functions)} tools converted to functions"
            )

            # Store connection status chunk to be yielded by the streaming method
            try:
                connected_server_names = (
                    mcp_client.get_server_names()
                    if hasattr(mcp_client, "get_server_names")
                    else []
                )
                if connected_server_names:
                    chunk = StreamChunk(
                        type="mcp_status",
                        status="mcp_connected",
                        content=f"✅ [MCP] Connected to {len(connected_server_names)} servers",
                        source="mcp_setup",
                    )
                    setattr(self.backend, "_mcp_connection_status_chunk", chunk)
            except Exception as e:
                logger.warning(f"Could not generate MCP connected status: {e}")

            # Record success for circuit breaker - only for actually connected servers
            _circuit_breakers_enabled = getattr(self.backend, "_circuit_breakers_enabled", False)
            if _circuit_breakers_enabled and circuit_breaker and mcp_client:
                try:
                    connected_server_names = (
                        mcp_client.get_server_names()
                        if hasattr(mcp_client, "get_server_names")
                        else []
                    )
                    # Only record success if we have actual connected servers
                    # This prevents recording success when connection failed but mcp_client was created
                    if connected_server_names and len(connected_server_names) > 0:
                        connected_server_configs = [
                            server
                            for server in servers_to_use
                            if server.get("name") in connected_server_names
                        ]
                        if connected_server_configs:
                            await MCPCircuitBreakerManager.record_success(
                                connected_server_configs,
                                circuit_breaker,
                                backend_name=self._get_backend_name(),
                                agent_id=self._get_agent_id(),
                            )

                    else:
                        # No servers actually connected, don't record success
                        logger.info(
                            f"No MCP servers successfully connected, skipping circuit breaker success recording"
                        )
                except Exception as cb_error:
                    logger.warning(
                        f"Failed to record circuit breaker success: {cb_error}"
                    )
            
            return True
            
        except Exception as e:
            # This attempt failed
            logger.warning(f"MCP setup attempt failed: {e}")
            return False

    async def execute_mcp_functions_and_recurse(
        self,
        current_messages: List[Dict],
        tools: List[Dict],
        client,
        captured_function_calls: List[Dict] = None,
        build_api_params_callback: Optional[Callable] = None,
        create_stream_callback: Optional[Callable] = None,
        detect_function_calls_callback: Optional[Callable] = None,
        process_chunk_callback: Optional[Callable] = None,
        format_tool_result_callback: Optional[Callable] = None,
        fallback_stream_callback: Optional[Callable] = None,
        **kwargs,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Execute MCP functions and handle recursive streaming using backend-provided callbacks.

        Args:
            current_messages: Current message history
            tools: Available tools
            client: Backend client instance
            captured_function_calls: Function calls detected from streaming (optional)
            build_api_params_callback: Build backend-specific API parameters
            create_stream_callback: Create a new streaming iterator using api_params
            detect_function_calls_callback: Detect/accumulate function calls from chunks
            process_chunk_callback: Convert non-function chunks to StreamChunk
            format_tool_result_callback: Format tool result messages for the backend
            fallback_stream_callback: Fallback streaming function for non-MCP calls
            **kwargs: Additional backend-specific parameters

        Yields:
            StreamChunk objects for streaming progress and results
        """

        async def _maybe_await(result):
            if inspect.iscoroutine(result):
                return await result
            return result

        updated_messages = current_messages.copy()

        # 1) If we have captured MCP function calls, execute them first
        if captured_function_calls:
            functions = self._get_functions()
            non_mcp_functions = [
                call
                for call in captured_function_calls
                if call["name"] not in functions
            ]
            if non_mcp_functions:
                logger.info(
                    f"Non-MCP function calls detected: {[call['name'] for call in non_mcp_functions]}. "
                    f"Falling back to standard streaming."
                )
                if fallback_stream_callback:
                    async for chunk in fallback_stream_callback(
                        updated_messages, non_mcp_functions
                    ):
                        yield chunk
                else:
                    logger.warning(
                        "No fallback streaming callback provided for non-MCP calls."
                    )
                    yield StreamChunk(type="done")
                return

            # Circuit breaker check
            circuit_breaker = self._get_circuit_breaker()
            _circuit_breakers_enabled = getattr(
                self.backend, "_circuit_breakers_enabled", False
            )
            if _circuit_breakers_enabled and circuit_breaker:
                # Use already normalized servers from setup, re-apply only circuit breaker filtering
                filtered_servers = (
                    MCPCircuitBreakerManager.apply_circuit_breaker_filtering(
                        self._last_mcp_servers_used,
                        circuit_breaker,
                        backend_name=self._get_backend_name(),
                        agent_id=self._get_agent_id(),
                    )
                )
                if not filtered_servers:
                    # All servers are blocked by circuit breaker - only show message once per session
                    if not getattr(
                        self.backend, "_mcp_circuit_breaker_notified", False
                    ):
                        logger.warning("All MCP servers blocked by circuit breaker")
                        yield StreamChunk(
                            type="mcp_status",
                            status="mcp_blocked",
                            content="⚠️ [MCP] All servers blocked by circuit breaker",
                            source="circuit_breaker",
                        )
                        setattr(self.backend, "_mcp_circuit_breaker_notified", True)
                    yield StreamChunk(type="done")
                    return

            mcp_functions_executed = False
            for call in captured_function_calls:
                function_name = call["name"]
                if function_name in functions:
                    yield StreamChunk(
                        type="mcp_status",
                        status="mcp_tool_called",
                        content=f"🔧 [MCP Tool] Calling {function_name}...",
                        source=f"mcp_{function_name}",
                    )
                    try:
                        result, result_obj = await self.execute_mcp_function_with_retry(
                            function_name, call["arguments"]
                        )
                        if isinstance(result, str) and result.startswith("Error:"):
                            logger.warning(
                                f"MCP function {function_name} failed after retries: {result}"
                            )
                            continue
                    except Exception as e:
                        logger.error(f"Unexpected error in MCP function execution: {e}")
                        continue

                    function_call_msg = {
                        "type": "function_call",
                        "call_id": call.get("call_id", ""),
                        "name": function_name,
                        "arguments": call.get("arguments", ""),
                    }
                    updated_messages.append(function_call_msg)
                    yield StreamChunk(
                        type="mcp_status",
                        status="function_call",
                        content=f"Arguments for Calling {function_name}: {call.get('arguments', '')}",
                        source=f"mcp_{function_name}",
                    )

                    # Format result message if callback provided
                    try:
                        result_content_str = str(result)
                        if format_tool_result_callback:
                            formatted_msg = await _maybe_await(
                                format_tool_result_callback(call, result_content_str)
                            )
                            function_output_msg = formatted_msg or {
                                "type": "function_call_output",
                                "call_id": call.get("call_id", ""),
                                "output": result_content_str,
                            }
                        else:
                            function_output_msg = {
                                "type": "function_call_output",
                                "call_id": call.get("call_id", ""),
                                "output": result_content_str,
                            }
                    except Exception:
                        function_output_msg = {
                            "type": "function_call_output",
                            "call_id": call.get("call_id", ""),
                            "output": str(result),
                        }

                    updated_messages.append(function_output_msg)
                    try:
                        pretty_result = str(result_obj.content[0].text)
                    except Exception:
                        pretty_result = str(result)
                    yield StreamChunk(
                        type="mcp_status",
                        status="function_call_output",
                        content=f"Results for Calling {function_name}: {pretty_result}",
                        source=f"mcp_{function_name}",
                    )

                    logger.info(
                        f"Executed MCP function {function_name} (stdio/streamable-http)"
                    )
                    yield StreamChunk(
                        type="mcp_status",
                        status="mcp_tool_response",
                        content=f"✅ [MCP Tool] {function_name} completed",
                        source=f"mcp_{function_name}",
                    )
                    mcp_functions_executed = True

            if mcp_functions_executed:
                max_history = self._get_max_mcp_message_history()
                updated_messages = MCPMessageManager.trim_message_history(
                    updated_messages, max_history
                )

        # 2) Stream a fresh response with callbacks
        if not (
            build_api_params_callback
            and create_stream_callback
            and detect_function_calls_callback
            and process_chunk_callback
        ):
            logger.warning("Required callbacks not provided for MCP streaming; ending.")
            yield StreamChunk(type="done")
            return

        api_params = await _maybe_await(
            build_api_params_callback(updated_messages, tools, kwargs)
        )
        stream = await _maybe_await(create_stream_callback(api_params))

        captured_in_iteration: List[Dict[str, Any]] = []
        current_function_call: Optional[Dict[str, Any]] = None
        response_completed = False

        async for chunk in stream:
            consumed = False
            try:
                result = detect_function_calls_callback(
                    chunk, current_function_call, captured_in_iteration
                )
                if inspect.iscoroutine(result):
                    (
                        current_function_call,
                        captured_in_iteration,
                        consumed,
                    ) = await result
                else:
                    current_function_call, captured_in_iteration, consumed = result
            except Exception:
                consumed = False

            if not consumed:
                processed = await _maybe_await(process_chunk_callback(chunk))
                if processed is not None:
                    yield processed

            if hasattr(chunk, "type") and chunk.type == "response.completed":
                response_completed = True
                break

        if response_completed:
            if captured_in_iteration:
                # Recurse with captured calls and updated messages
                async for next_chunk in self.execute_mcp_functions_and_recurse(
                    updated_messages,
                    tools,
                    client,
                    captured_function_calls=captured_in_iteration,
                    build_api_params_callback=build_api_params_callback,
                    create_stream_callback=create_stream_callback,
                    detect_function_calls_callback=detect_function_calls_callback,
                    process_chunk_callback=process_chunk_callback,
                    format_tool_result_callback=format_tool_result_callback,
                    **kwargs,
                ):
                    yield next_chunk
                return
            else:
                yield StreamChunk(
                    type="mcp_status",
                    status="mcp_session_complete",
                    content="✅ [MCP] Session completed",
                    source="mcp_session",
                )
                yield StreamChunk(type="done")
                return

        # Safety net: if stream ended without explicit completion
        yield StreamChunk(type="done")
        return

    async def handle_mcp_error_and_fallback(
        self,
        error: Exception,
        api_params: Dict[str, Any],
        provider_tools: List[Dict[str, Any]],
        stream_func: Callable[[Dict[str, Any]], AsyncGenerator[StreamChunk, None]],
        *,
        is_multi_agent_context: bool = False,
        is_final_agent: bool = False,
        original_tools: Optional[List[Dict[str, Any]]] = None,
        existing_answers_available: bool = False,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Handle MCP errors with fallback to non-MCP streaming.

        Args:
            error: The exception that occurred
            api_params: Original API parameters
            provider_tools: Provider-specific tools for fallback
            stream_func: Fallback streaming function
            is_multi_agent_context: Whether this is in a multi-agent context
            is_final_agent: Whether this is the final agent in multi-agent orchestration
            original_tools: Original tools before MCP filtering
            existing_answers_available: Whether existing answers are available for voting

        Yields:
            StreamChunk objects for error handling and fallback streaming
        """
        # Update failure statistics
        async with self._get_stats_lock():
            failures = self._get_mcp_tool_failures() + 1
            self._set_mcp_tool_failures(failures)
            call_index_snapshot = getattr(self.backend, "_mcp_tool_calls_count", 0)

        # Get error details using standardized error handler
        log_type, user_message, _ = MCPErrorHandler.get_error_details(error)

        logger.warning(
            f"MCP tool call #{call_index_snapshot} failed - {log_type}: {error}"
        )

        # Yield user-friendly error message
        yield StreamChunk(
            type="mcp_status",
            status="mcp_error",
            content=user_message,
            source="mcp_error",
        )
        yield StreamChunk(
            type="content",
            content=f"\n⚠️  {user_message} ({error}); continuing without MCP tools\n",
        )

        # Build non-MCP configuration and stream fallback
        fallback_params = dict(api_params)

        # Remove any MCP tools from the tools list
        if "tools" in fallback_params:
            non_mcp_tools = [
                tool for tool in fallback_params["tools"] if tool.get("type") != "mcp"
            ]
            fallback_params["tools"] = non_mcp_tools

        # Also remove any tools that are in the functions registry (MCP tools)
        if "tools" in fallback_params:
            functions = self._get_functions()
            non_mcp_tools = [
                tool
                for tool in fallback_params["tools"]
                if tool.get("name") not in functions
            ]
            fallback_params["tools"] = non_mcp_tools

        # Intelligent coordination tool filtering based on context
        if is_multi_agent_context and original_tools:
            filtered_coordination_tools = self._filter_coordination_tools_for_context(
                fallback_params.get("tools", []),
                original_tools,
                existing_answers_available=existing_answers_available,
                is_final_agent=is_final_agent,
            )
            fallback_params["tools"] = filtered_coordination_tools

        # Add back provider tools if they were present
        if provider_tools:
            if "tools" not in fallback_params:
                fallback_params["tools"] = []

            # Deduplicate by (tool.get("type"), tool.get("name")) to avoid duplicates
            existing_tools = {
                (tool.get("type"), tool.get("name"))
                for tool in fallback_params["tools"]
            }
            deduplicated_provider_tools = [
                tool
                for tool in provider_tools
                if (tool.get("type"), tool.get("name")) not in existing_tools
            ]
            fallback_params["tools"].extend(deduplicated_provider_tools)

        async for chunk in stream_func(fallback_params):
            yield chunk

    def _filter_coordination_tools_for_context(
        self,
        current_tools: List[Dict[str, Any]],
        original_tools: List[Dict[str, Any]],
        *,
        existing_answers_available: bool = False,
        is_final_agent: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Filter coordination tools based on current context to prevent conflicts.

        Args:
            current_tools: Current list of tools after MCP filtering
            original_tools: Original tools before MCP filtering
            existing_answers_available: Whether existing answers are available for voting
            is_final_agent: Whether this is the final agent in multi-agent orchestration

        Returns:
            Filtered list of tools that prevents vote/new_answer conflicts
        """
        # Identify coordination tools in the current tools list
        coordination_tools = []
        other_tools = []

        for tool in current_tools:
            tool_name = tool.get("name", "")
            if self._is_coordination_tool(tool_name):
                coordination_tools.append(tool)
            else:
                other_tools.append(tool)

        # If no coordination tools, return as-is
        if not coordination_tools:
            return current_tools

        # Context-aware tool filtering logic
        filtered_coordination_tools = []

        for tool in coordination_tools:
            tool_name = tool.get("name", "")

            # Only preserve coordination tools that are appropriate for the context
            if self._should_preserve_coordination_tool(
                tool_name,
                original_tools,
                existing_answers_available=existing_answers_available,
                is_final_agent=is_final_agent,
            ):
                filtered_coordination_tools.append(tool)

        # Combine filtered coordination tools with other tools
        return other_tools + filtered_coordination_tools

    def _is_coordination_tool(self, tool_name: str) -> bool:
        """Check if a tool name is a coordination tool."""
        coordination_tool_names = {"vote", "new_answer"}
        return tool_name.lower() in coordination_tool_names

    def _should_preserve_coordination_tool(
        self,
        tool_name: str,
        original_tools: List[Dict[str, Any]],
        *,
        existing_answers_available: bool = False,
        is_final_agent: bool = False,
    ) -> bool:
        """
        Determine if a coordination tool should be preserved based on context.

        Args:
            tool_name: Name of the coordination tool
            original_tools: Original tools list for context
            existing_answers_available: Whether existing answers are available
            is_final_agent: Whether this is the final agent

        Returns:
            True if the tool should be preserved, False otherwise
        """
        if not self._is_coordination_tool(tool_name):
            return False

        # Check if the tool was originally available
        original_tool_names = {tool.get("name", "") for tool in original_tools}
        if tool_name not in original_tool_names:
            return False

        # Context-aware preservation logic
        if existing_answers_available:
            # If answers exist, only preserve 'vote' tool
            return tool_name.lower() == "vote"
        else:
            # If no answers exist, only preserve 'new_answer' tool
            return tool_name.lower() == "new_answer"

    def _get_backend_name(self) -> str:
        """Get the backend name for MCP operations."""
        return self.backend.get_provider_name()

    def _get_agent_id(self) -> Optional[str]:
        """Get the agent ID for MCP operations."""
        return getattr(self.backend, "agent_id", None)

    def _get_mcp_client(self) -> Optional[MultiMCPClient]:
        """Get the MCP client instance."""
        return getattr(self.backend, "_mcp_client", None)

    def _get_circuit_breaker(self) -> Optional[Any]:
        """Get the circuit breaker instance."""
        return getattr(self.backend, "_mcp_tools_circuit_breaker", None)

    def _get_stats_lock(self) -> asyncio.Lock:
        """Get the statistics lock."""
        return getattr(self.backend, "_stats_lock", asyncio.Lock())

    def _get_max_mcp_message_history(self) -> int:
        """Get the maximum MCP message history limit."""
        return getattr(self.backend, "_max_mcp_message_history", 200)

    def _get_functions(self) -> Dict[str, Function]:
        """Get the functions registry."""
        return getattr(self.backend, "functions", {})

    def _get_mcp_tool_failures(self) -> int:
        """Get the current MCP tool failure count."""
        return getattr(self.backend, "_mcp_tool_failures", 0)

    def _set_mcp_tool_failures(self, count: int) -> None:
        """Set the MCP tool failure count."""
        setattr(self.backend, "_mcp_tool_failures", count)

    def _set_mcp_client(self, client: Optional[MultiMCPClient]) -> None:
        """Set the MCP client instance."""
        setattr(self.backend, "_mcp_client", client)

    def _set_functions(self, functions: Dict[str, Function]) -> None:
        """Set the functions registry."""
        setattr(self.backend, "functions", functions)

    async def execute_mcp_function_with_retry(
        self, function_name: str, arguments_json: str, max_retries: int = 3
    ) -> tuple:
        """Execute MCP function with exponential backoff retry logic."""

        # Convert JSON string to dict for shared utility
        try:
            args = json.loads(arguments_json)
        except (json.JSONDecodeError, ValueError) as e:
            error_str = f"Error: Invalid JSON arguments: {e}"
            return error_str, {"error": error_str}

        # Stats callback for tracking
        async def stats_callback(action: str) -> int:
            async with self._get_stats_lock():
                if action == "increment_calls":
                    call_count = getattr(self.backend, "_mcp_tool_calls_count", 0) + 1
                    setattr(self.backend, "_mcp_tool_calls_count", call_count)
                    return call_count
                elif action == "increment_failures":
                    failures = self._get_mcp_tool_failures() + 1
                    self._set_mcp_tool_failures(failures)
                    return failures
            return 0

        # Circuit breaker callback
        async def circuit_breaker_callback(event: str, error_msg: str) -> None:
            circuit_breaker = self._get_circuit_breaker()
            if circuit_breaker:
                # For individual function calls, we don't have server configurations readily available
                # The circuit breaker manager should handle this gracefully with empty server list
                if event == "failure":
                    await MCPCircuitBreakerManager.record_event(
                        [],
                        circuit_breaker,
                        "failure",
                        error_msg,
                        backend_name=self._get_backend_name(),
                        agent_id=self._get_agent_id(),
                        backend_instance=self.backend,
                    )
                else:
                    await MCPCircuitBreakerManager.record_event(
                        [],
                        circuit_breaker,
                        "success",
                        backend_name=self._get_backend_name(),
                        agent_id=self._get_agent_id(),
                        backend_instance=self.backend,
                    )

        result = await self._execution_manager.execute_function_with_retry(
            function_name=function_name,
            args=args,
            functions=self._get_functions(),
            max_retries=max_retries,
            stats_callback=stats_callback,
            circuit_breaker_callback=circuit_breaker_callback,
            logger_instance=logger,
        )

        # Convert result to string for response.py compatibility
        if isinstance(result, dict) and "error" in result:
            return f"Error: {result['error']}", result
        return str(result), result

    def setup_circuit_breaker(self) -> None:
        """
        Initialize MCP tools circuit breaker configuration.

        This method consolidates circuit breaker setup logic that was duplicated
        across multiple backend implementations.
        """
        # Check if circuit breakers are available
        if MCPCircuitBreaker is None:
            self.logger.warning(
                "MCPCircuitBreaker not available - proceeding without circuit breaker protection"
            )
            setattr(self.backend, "_circuit_breakers_enabled", False)
            return

        # Initialize circuit breaker status
        setattr(self.backend, "_circuit_breakers_enabled", True)

        # Build circuit breaker configuration using shared utility
        mcp_tools_config = MCPConfigHelper.build_circuit_breaker_config("mcp_tools")

        if mcp_tools_config:
            setattr(
                self.backend,
                "_mcp_tools_circuit_breaker",
                MCPCircuitBreaker(mcp_tools_config),
            )
            self.logger.info("Circuit breaker initialized for MCP tools")
        else:
            self.logger.warning(
                "MCP tools circuit breaker config not available, disabling circuit breaker functionality"
            )
            setattr(self.backend, "_circuit_breakers_enabled", False)
            setattr(self.backend, "_mcp_tools_circuit_breaker", None)

    async def setup_context_manager(self) -> None:
        """
        Setup MCP resources during async context manager entry.

        This method directly implements the logic previously delegated to MCPResourceManager.
        """
        backend_name = self._get_backend_name()
        agent_id = self._get_agent_id()

        # Setup MCP tools if configured during context manager entry
        if hasattr(self.backend, "mcp_servers") and self.backend.mcp_servers:
            try:
                # Directly call our own setup method to avoid delegation loops
                await self.setup_mcp_tools()
            except Exception as e:
                log_mcp_activity(
                    backend_name,
                    "setup failed during context entry",
                    {"error": str(e)},
                    agent_id=agent_id,
                )

    async def cleanup_context_manager(self, logger_instance=None) -> None:
        """
        Clean up MCP resources during async context manager exit.

        This method directly implements the logic previously delegated to MCPResourceManager.
        """
        log = logger_instance or self.logger
        backend_name = self._get_backend_name()
        agent_id = self._get_agent_id()

        try:
            if hasattr(self.backend, "cleanup_mcp"):
                await self.backend.cleanup_mcp()
            elif hasattr(self.backend, "_mcp_client"):
                mcp_client = getattr(self.backend, "_mcp_client", None)
                if mcp_client:
                    try:
                        await mcp_client.disconnect()
                        log_mcp_activity(
                            backend_name,
                            "client cleanup completed",
                            {},
                            agent_id=agent_id,
                        )
                    except Exception as e:
                        log_mcp_activity(
                            backend_name,
                            "error during client cleanup",
                            {"error": str(e)},
                            agent_id=agent_id,
                        )

                # Reset backend state
                setattr(self.backend, "_mcp_client", None)
                setattr(self.backend, "_mcp_state", MCPState.NOT_INITIALIZED)
                if hasattr(self.backend, "functions"):
                    self.backend.functions.clear()
        except Exception as e:
            log.error(f"Error during MCP cleanup for backend '{backend_name}': {e}")
            log_mcp_activity(
                backend_name,
                "error during cleanup",
                {"error": str(e)},
                agent_id=agent_id,
            )

    async def cleanup_mcp(self) -> None:
        """
        Clean up MCP client connections and reset state.

        This consolidates the cleanup logic that was duplicated across backends.
        """
        mcp_client = self._get_mcp_client()
        backend_name = self._get_backend_name()
        agent_id = self._get_agent_id()

        if mcp_client:
            try:
                await mcp_client.disconnect()
                log_mcp_activity(
                    backend_name, "client cleanup completed", {}, agent_id=agent_id
                )
            except Exception as e:
                log_mcp_activity(
                    backend_name,
                    "error during client cleanup",
                    {"error": str(e)},
                    agent_id=agent_id,
                )

            # Reset backend state
            self._set_mcp_client(None)
            setattr(self.backend, "_mcp_state", MCPState.NOT_INITIALIZED)
            self._set_functions({})
            self.logger.info("MCP client cleaned up")