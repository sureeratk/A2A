import json
import logging

from collections.abc import AsyncGenerator
from typing import Any

from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from a2a.server.errors import MethodNotImplementedError
from a2a.server.request_handlers.request_handler import A2ARequestHandler
from a2a.types import (
    A2AError,
    A2ARequest,
    AgentCard,
    CancelTaskRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    InternalError,
    InvalidRequestError,
    JSONParseError,
    JSONRPCError,
    JSONRPCErrorResponse,
    JSONRPCResponse,
    SendMessageRequest,
    SendStreamingMessageRequest,
    SendStreamingMessageResponse,
    SetTaskPushNotificationConfigRequest,
    TaskResubscriptionRequest,
    UnsupportedOperationError,
)


logger = logging.getLogger(__name__)


class A2AApplication:
    """A Starlette application implementing the A2A protocol server endpoints.

    Handles incoming JSON-RPC requests, routes them to the appropriate
    handler methods, and manages response generation including Server-Sent Events (SSE).
    """

    def __init__(
        self, agent_card: AgentCard, request_handler: A2ARequestHandler
    ):
        """Initializes the A2AApplication.

        Args:
            agent_card: The AgentCard describing the agent's capabilities.
            request_handler: The handler instance responsible for processing A2A requests.
        """
        self.agent_card = agent_card
        self.request_handler = request_handler

    def _generate_error_response(
        self, request_id: str | int | None, error: JSONRPCError | A2AError
    ) -> JSONResponse:
        """Creates a JSONResponse for a JSON-RPC error."""
        error_resp = JSONRPCErrorResponse(
            id=request_id,
            error=error if isinstance(error, JSONRPCError) else error.root,
        )

        log_level = (
            logging.ERROR
            if not isinstance(error, A2AError)
            or isinstance(error.root, InternalError)
            else logging.WARNING
        )
        error_details = f"Code={error_resp.error.code}, Message='{error_resp.error.message}'"
        if error_resp.error.data is not None:
            error_details += f', Data={error_resp.error.data!s}'

        logger.log(
            log_level,
            f'Request Error (ID: {request_id}): {error_details}',
        )
        return JSONResponse(
            error_resp.model_dump(mode='json', exclude_none=True)
        )

    async def _handle_requests(self, request: Request) -> Response:
        """Handles incoming POST requests to the main A2A endpoint.

        Parses the request body as JSON, validates it against A2A request types,
        dispatches it to the appropriate handler method, and returns the response.
        Handles JSON parsing errors, validation errors, and other exceptions,
        returning appropriate JSON-RPC error responses.
        """
        request_id = None
        try:
            body = await request.json()
            a2a_request = A2ARequest.model_validate(body)

            request_id = a2a_request.root.id
            request_obj = a2a_request.root

            logger.info(
                f'Processing request ID: {request_id}, Method: {request_obj.method}'
            )

            if isinstance(
                request_obj,
                TaskResubscriptionRequest | SendStreamingMessageRequest,
            ):
                return await self._process_streaming_request(
                    request_id, a2a_request
                )

            return await self._process_non_streaming_request(
                request_id, a2a_request
            )
        except MethodNotImplementedError:
            return self._generate_error_response(
                request_id, A2AError(UnsupportedOperationError())
            )
        except json.decoder.JSONDecodeError as e:
            return self._generate_error_response(
                None, A2AError(JSONParseError(message=str(e)))
            )
        except ValidationError as e:
            return self._generate_error_response(
                request_id,
                A2AError(InvalidRequestError(data=json.loads(e.json()))),
            )
        except Exception as e:
            logger.error(
                f'Unhandled exception during request (ID: {request_id}): {e}',
                exc_info=True,
            )
            return self._generate_error_response(
                request_id, A2AError(InternalError(message=str(e)))
            )

    async def _process_streaming_request(
        self, request_id: str | int | None, a2a_request: A2ARequest
    ) -> Response:
        """Processes streaming requests.

        Args:
            request_id: The ID of the request.
            a2a_request: The validated A2ARequest object.
        """
        logger.debug(
            'Processing the streaming request with id %s and type %s',
            request_id,
            type(a2a_request.root),
        )
        request_obj = a2a_request.root
        handler_result: Any = None
        if isinstance(
            request_obj,
            SendStreamingMessageRequest,
        ):
            handler_result = self.request_handler.on_message_send_stream(
                request_obj
            )
        elif isinstance(request_obj, TaskResubscriptionRequest):
            handler_result = self.request_handler.on_resubscribe_to_task(
                request_obj
            )

        return self._create_response(handler_result)

    async def _process_non_streaming_request(
        self, request_id: str | int | None, a2a_request: A2ARequest
    ) -> Response:
        """Processes non-streaming requests.

        Args:
            request_id: The ID of the request.
            a2a_request: The validated A2ARequest object.
        """
        logger.debug(
            'Processing the non-streaming request with id %s and type %s',
            request_id,
            type(a2a_request.root),
        )
        request_obj = a2a_request.root
        handler_result: Any = None
        match request_obj:
            case SendMessageRequest():
                handler_result = await self.request_handler.on_message_send(
                    request_obj
                )
            case CancelTaskRequest():
                handler_result = await self.request_handler.on_cancel_task(
                    request_obj
                )
            case GetTaskRequest():
                handler_result = await self.request_handler.on_get_task(
                    request_obj
                )
            case SetTaskPushNotificationConfigRequest():
                handler_result = await self.request_handler.on_set_task_push_notification_config(
                    request_obj
                )
            case GetTaskPushNotificationConfigRequest():
                handler_result = await self.request_handler.on_get_task_push_notification_config(
                    request_obj
                )
            case _:
                logger.error(
                    f'Unhandled validated request type: {type(request_obj)}',
                    exc_info=False,
                )
                error = UnsupportedOperationError(
                    message=f'Request type {type(request_obj).__name__} is unknown.'
                )
                handler_result = JSONRPCErrorResponse(
                    id=request_id, error=error
                )

        return self._create_response(handler_result)

    def _create_response(
        self,
        handler_result: AsyncGenerator[SendStreamingMessageResponse, None]
        | JSONRPCErrorResponse
        | JSONRPCResponse,
    ) -> Response:
        """Creates a Starlette Response based on the result from the request handler.

        Handles:
        - AsyncGenerator for Server-Sent Events (SSE).
        - JSONRPCErrorResponse for explicit errors returned by handlers.
        - Pydantic RootModels (like GetTaskResponse) containing success or error payloads.
        - Unexpected types by returning an InternalError.

        Args:
            handler_result: The object returned by the A2ARequestHandler method.

        Returns:
            A Starlette JSONResponse or EventSourceResponse.
        """
        if isinstance(handler_result, AsyncGenerator):
            # Result is a stream of SendStreamingMessageResponse objects
            logger.debug('Creating EventSourceResponse for streaming data.')

            async def event_generator(
                stream: AsyncGenerator[SendStreamingMessageResponse, None],
            ) -> AsyncGenerator[dict[str, str], None]:
                async for item in stream:
                    yield {'data': item.root.model_dump_json(exclude_none=True)}

                logger.debug('Streaming completed')

            return EventSourceResponse(event_generator(handler_result))

        if isinstance(handler_result, JSONRPCErrorResponse):
            logger.debug('Returning error response.')
            return JSONResponse(
                handler_result.model_dump(
                    mode='json',
                    exclude_none=True,
                )
            )

        return JSONResponse(
            handler_result.root.model_dump(mode='json', exclude_none=True)
        )

    async def _handle_get_agent_card(self, request: Request) -> JSONResponse:
        """Handles GET requests for the agent card."""
        logger.info(f'Serving the agent card at {request.url.path}')
        return JSONResponse(
            self.agent_card.model_dump(mode='json', exclude_none=True)
        )

    def build(
        self,
        agent_card_url: str = '/.well-known/agent.json',
        rpc_url: str = '/',
        **kwargs: Any,
    ) -> Starlette:
        """Builds and returns the Starlette application instance.

        Args:
            agent_card_url: The URL for the agent card endpoint.
            rpc_url: The URL for the A2A JSON-RPC endpoint
            **kwargs: Additional keyword arguments to pass to the Starlette constructor.

        Returns:
            A configured Starlette application instance.
        """
        logger.info('Building A2A Application instance')
        default_routes = [
            Route(
                rpc_url,
                self._handle_requests,
                methods=['POST'],
                name='a2a_handler',
            ),
            Route(
                agent_card_url,
                self._handle_get_agent_card,
                methods=['GET'],
                name='agent_card',
            ),
        ]
        provided_routes = kwargs.pop('routes', [])
        all_routes = provided_routes + default_routes

        return Starlette(routes=all_routes, **kwargs)
