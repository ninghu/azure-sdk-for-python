# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

from ast import literal_eval
import copy
import time
import asyncio
import uuid
import logging
from urllib.parse import urlparse
from abc import ABC, abstractmethod
from typing import Deque, Dict, List, Optional, Union
from collections import deque

from aiohttp import TraceConfig  # pylint: disable=networking-import-outside-azure-core-transport
from aiohttp.web import HTTPException  # pylint: disable=networking-import-outside-azure-core-transport
from aiohttp_retry import RetryClient, RandomRetry  # pylint: disable=networking-import-outside-azure-core-transport

from .identity_manager import APITokenManager
from .images import replace_prompt_captions, format_multimodal_prompt


MIN_ERRORS_TO_FAIL = 3
MAX_TIME_TAKEN_RECORDS = 20_000


def get_model_class_from_url(endpoint_url: str) -> type:
    """
    Convert an endpoint URL to the appropriate model class.

    :param endpoint_url: The URL of the endpoint.
    :type endpoint_url: str
    :return: The model class corresponding to the endpoint URL.
    :rtype: type
    """
    endpoint_path = urlparse(endpoint_url).path  # remove query params

    if endpoint_path.endswith("chat/completions"):
        return OpenAIChatCompletionsModel
    if "/rainbow" in endpoint_path:
        return OpenAIMultiModalCompletionsModel
    if endpoint_path.endswith("completions"):
        return OpenAICompletionsModel
    raise ValueError(f"Unknown API type for endpoint {endpoint_url}")


# ===================== HTTP Retry ======================
class AsyncHTTPClientWithRetry:
    def __init__(self, n_retry, retry_timeout, logger, retry_options=None):
        self.attempts = n_retry
        self.logger = logger

        # Set up async HTTP client with retry

        trace_config = TraceConfig()  # set up request logging
        trace_config.on_request_start.append(self.on_request_start)
        trace_config.on_request_end.append(self.on_request_end)
        if retry_options is None:
            retry_options = RandomRetry(  # set up retry configuration
                statuses=[104, 408, 409, 424, 429, 500, 502, 503, 504],  # on which statuses to retry
                attempts=n_retry,
                min_timeout=retry_timeout,
                max_timeout=retry_timeout,
            )

        self.client = RetryClient(trace_configs=[trace_config], retry_options=retry_options)

    async def on_request_start(self, trace_config_ctx, params):
        current_attempt = trace_config_ctx.trace_request_ctx["current_attempt"]
        self.logger.info("[ATTEMPT %s] Sending %s request to %s" % (current_attempt, params.method, params.url))

    async def on_request_end(self, trace_config_ctx, params):
        current_attempt = trace_config_ctx.trace_request_ctx["current_attempt"]
        request_headers = dict(params.response.request_info.headers)
        if "Authorization" in request_headers:
            del request_headers["Authorization"]  # hide auth token from logs
        if "api-key" in request_headers:
            del request_headers["api-key"]
        self.logger.info(
            "[ATTEMPT %s] For %s request to %s, received response with status %s and request headers: %s"
            % (current_attempt, params.method, params.url, params.response.status, request_headers)
        )


# ===========================================================
# ===================== LLMBase Class =======================
# ===========================================================


class LLMBase(ABC):
    """
    Base class for all LLM models.
    """

    def __init__(self, endpoint_url: str, name: str = "unknown", additional_headers: Optional[dict] = None):
        if additional_headers is None:
            additional_headers = {}
        self.endpoint_url = endpoint_url
        self.name = name
        self.additional_headers = additional_headers
        self.logger = logging.getLogger(repr(self))

        # Metric tracking
        self.lock = asyncio.Lock()
        self.response_times: Deque[Union[int, float]] = deque(maxlen=MAX_TIME_TAKEN_RECORDS)
        self.step = 0
        self.error_count = 0

    @abstractmethod
    def get_model_params(self) -> dict:
        pass

    @abstractmethod
    def format_request_data(self, prompt: str, **request_params) -> dict:
        pass

    async def get_completion(
        self,
        prompt: str,
        session: RetryClient,
        **request_params,
    ) -> dict:
        """
        Query the model a single time with a prompt.

        :param prompt: Prompt str to query model with.
        :type prompt: str
        :param session: aiohttp RetryClient object to use for the request.
        :type session: RetryClient
        :keyword **request_params: Additional parameters to pass to the request.
        :return: Dictionary containing the completion response from the model.
        :rtype: dict
        """
        request_data = self.format_request_data(prompt, **request_params)
        return await self.request_api(
            session=session,
            request_data=request_data,
        )

    @abstractmethod
    async def get_all_completions(
        self,
        prompts: List[str],
        session: RetryClient,
        api_call_max_parallel_count: int,
        api_call_delay_seconds: float,
        request_error_rate_threshold: float,
        **request_params,
    ) -> List[dict]:
        pass

    @abstractmethod
    async def request_api(
        self,
        session: RetryClient,
        request_data: dict,
    ) -> dict:
        pass

    @abstractmethod
    async def get_conversation_completion(
        self,
        messages: List[dict],
        session: RetryClient,
        role: str,
        **request_params,
    ) -> dict:
        pass

    @abstractmethod
    async def request_api_parallel(
        self,
        request_datas: List[dict],
        output_collector: List,
        session: RetryClient,
        api_call_delay_seconds: float,
        request_error_rate_threshold: float,
    ) -> None:
        pass

    def _log_request(self, request: dict) -> None:
        self.logger.info("Request: %s", request)

    async def _add_successful_response(self, time_taken: Union[int, float]) -> None:
        async with self.lock:
            self.response_times.append(time_taken)
            self.step += 1

    async def _add_error(self) -> None:
        async with self.lock:
            self.error_count += 1
            self.step += 1

    async def get_response_count(self) -> int:
        async with self.lock:
            return len(self.response_times)

    async def get_response_times(self) -> List[float]:
        async with self.lock:
            return list(self.response_times)

    async def get_average_response_time(self) -> float:
        async with self.lock:
            return sum(self.response_times) / len(self.response_times)

    async def get_error_rate(self) -> float:
        async with self.lock:
            return self.error_count / self.step

    async def get_error_count(self) -> int:
        async with self.lock:
            return self.error_count

    def __repr__(self):
        return f"{self.__class__.__name__}(name={self.name})"


# ===========================================================
# ================== OpenAICompletions ======================
# ===========================================================


class OpenAICompletionsModel(LLMBase):  # pylint: disable=too-many-instance-attributes
    """
    Object for calling a Completions-style API for OpenAI models.
    """

    prompt_idx_key = "__prompt_idx__"

    max_stop_tokens = 4
    stop_tokens = ["<|im_end|>", "<|endoftext|>"]

    model_param_names = [
        "model",
        "temperature",
        "max_tokens",
        "top_p",
        "n",
        "frequency_penalty",
        "presence_penalty",
        "stop",
    ]

    CHAT_START_TOKEN = "<|im_start|>"
    CHAT_END_TOKEN = "<|im_end|>"

    def __init__(
        self,
        *,
        endpoint_url: str,
        name: str = "OpenAICompletionsModel",
        additional_headers: Optional[dict] = None,
        api_version: Optional[str] = "2023-03-15-preview",
        token_manager: APITokenManager,
        azureml_model_deployment: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = 0.7,
        max_tokens: Optional[int] = 300,
        top_p: Optional[float] = None,  # Recommended to use top_p or temp, not both
        n: Optional[int] = 1,
        frequency_penalty: Optional[float] = 0,
        presence_penalty: Optional[float] = 0,
        stop: Optional[Union[List[str], str]] = None,
        image_captions: Optional[Dict[str, str]] = None,
        # pylint: disable=unused-argument
        images_dir: Optional[str] = None,  # Note: unused, kept for class compatibility
    ):
        if additional_headers is None:
            additional_headers = {}
        super().__init__(endpoint_url=endpoint_url, name=name, additional_headers=additional_headers)
        self.api_version = api_version
        self.token_manager = token_manager
        self.azureml_model_deployment = azureml_model_deployment
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.n = n
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.image_captions = image_captions if image_captions is not None else {}

        # Default stop to end token if not provided
        if not stop:
            stop = []
        # Else if stop sequence is given as a string (Ex: "["\n", "<im_end>"]"), convert
        elif isinstance(stop, str) and stop.startswith("[") and stop.endswith("]"):
            stop = literal_eval(stop)
        elif isinstance(stop, str):
            stop = [stop]
        self.stop: List = stop  # type: ignore[assignment]

        # If stop tokens do not include default end tokens, add them
        for token in self.stop_tokens:
            if len(self.stop) >= self.max_stop_tokens:
                break
            if token not in self.stop:
                self.stop.append(token)

        if top_p not in [None, 1.0] and temperature is not None:
            self.logger.warning(
                "Both top_p and temperature are set.  OpenAI advises against using both at the same time."
            )

        self.logger.info("Default model settings: %s", self.get_model_params())

    def get_model_params(self):
        return {param: getattr(self, param) for param in self.model_param_names if getattr(self, param) is not None}

    def format_request_data(self, prompt: str, **request_params) -> Dict[str, str]:
        """
        Format the request data for the OpenAI API.

        :param prompt: The prompt string.
        :type prompt: str
        :keyword request_params: Additional parameters to pass to the model.
        :return: The formatted request data.
        :rtype: Dict[str, str]
        """
        # Caption images if available
        if len(self.image_captions.keys()):
            prompt = replace_prompt_captions(
                prompt=prompt,
                captions=self.image_captions,
            )

        request_data = {"prompt": prompt, **self.get_model_params()}
        request_data.update(request_params)
        return request_data

    async def get_conversation_completion(
        self,
        messages: List[dict],
        session: RetryClient,
        role: str = "assistant",
        **request_params,
    ) -> dict:
        """
        Query the model a single time with a message.

        :param messages: List of messages to query the model with.
                         Expected format: [{"role": "user", "content": "Hello!"}, ...]
        :type messages: List[dict]
        :param session: aiohttp RetryClient object to query the model with.
        :type session: RetryClient
        :param role: Role of the user sending the message.
        :type role: str
        :keyword request_params: Additional parameters to pass to the model.
        :return: Dictionary containing the completion response from the model.
        :rtype: dict
        """
        prompt = []
        for message in messages:
            prompt.append(f"{self.CHAT_START_TOKEN}{message['role']}\n{message['content']}\n{self.CHAT_END_TOKEN}\n")
        prompt_string: str = "".join(prompt)
        prompt_string += f"{self.CHAT_START_TOKEN}{role}\n"

        return await self.get_completion(
            prompt=prompt_string,
            session=session,
            **request_params,
        )

    async def get_all_completions(  # type: ignore[override]
        self,
        prompts: List[Dict[str, str]],
        session: RetryClient,
        api_call_max_parallel_count: int = 1,
        api_call_delay_seconds: float = 0.1,
        request_error_rate_threshold: float = 0.5,
        **request_params,
    ) -> List[dict]:
        """
        Run a batch of prompts through the model and return the results in the order given.

        :param prompts: List of prompts to query the model with.
        :type prompts: List[Dict[str, str]]
        :param session: aiohttp RetryClient to use for the request.
        :type session: RetryClient
        :param api_call_max_parallel_count: Number of parallel requests to make to the API.
        :type api_call_max_parallel_count: int
        :param api_call_delay_seconds: Number of seconds to wait between API requests.
        :type api_call_delay_seconds: float
        :param request_error_rate_threshold: Maximum error rate allowed before raising an error.
        :type request_error_rate_threshold: float
        :keyword request_params: Additional parameters to pass to the API.
        :return: List of completion results.
        :rtype: List[dict]
        """
        if api_call_max_parallel_count > 1:
            self.logger.info("Using %s parallel workers to query the API..", api_call_max_parallel_count)

        # Format prompts and tag with index
        request_datas: List[Dict] = []
        for idx, prompt in enumerate(prompts):
            prompt: Dict[str, str] = self.format_request_data(  # type: ignore[no-redef]
                prompt, **request_params  # type: ignore[arg-type]
            )
            prompt[self.prompt_idx_key] = idx  # type: ignore[assignment]
            request_datas.append(prompt)

        # Perform inference
        if len(prompts) == 0:
            return []  # queue is empty

        output_collector: List = []
        tasks = [  # create a set of worker-tasks to query inference endpoint in parallel
            asyncio.create_task(
                self.request_api_parallel(
                    request_datas=request_datas,
                    output_collector=output_collector,
                    session=session,
                    api_call_delay_seconds=api_call_delay_seconds,
                    request_error_rate_threshold=request_error_rate_threshold,
                )
            )
            for _ in range(api_call_max_parallel_count)
        ]

        # Await the completion of all tasks, and propagate any exceptions
        await asyncio.gather(*tasks, return_exceptions=False)
        if request_datas:
            raise RuntimeError("All inference tasks were finished, but the queue is not empty")

        # Output results back to the caller
        output_collector.sort(key=lambda x: x[self.prompt_idx_key])
        for output in output_collector:
            output.pop(self.prompt_idx_key)
        return output_collector

    async def request_api_parallel(
        self,
        request_datas: List[dict],
        output_collector: List,
        session: RetryClient,
        api_call_delay_seconds: float = 0.1,
        request_error_rate_threshold: float = 0.5,
    ) -> None:
        """
        Query the model for all prompts given as a list and append the output to output_collector.

        :param request_datas: List of request data dictionaries.
        :type request_datas: List[dict]
        :param output_collector: List to store the output.
        :type output_collector: List
        :param session: RetryClient session.
        :type session: RetryClient
        :param api_call_delay_seconds: Delay between consecutive API calls in seconds.
        :type api_call_delay_seconds: float, optional
        :param request_error_rate_threshold: Threshold for request error rate.
        :type request_error_rate_threshold: float, optional
        """
        logger_tasks: List = []  # to await for logging to finish

        while True:  # process data from queue until it's empty
            try:
                request_data = request_datas.pop()
                prompt_idx = request_data.pop(self.prompt_idx_key)

                try:
                    response = await self.request_api(
                        session=session,
                        request_data=request_data,
                    )
                    await self._add_successful_response(response["time_taken"])
                except HTTPException as e:
                    response = {
                        "request": request_data,
                        "response": {
                            "finish_reason": "error",
                            "error": str(e),
                        },
                    }
                    await self._add_error()

                    self.logger.exception("Errored on prompt #%s", str(prompt_idx))

                    # if we count too many errors, we stop and raise an exception
                    response_count = await self.get_response_count()
                    error_rate = await self.get_error_rate()
                    if response_count >= MIN_ERRORS_TO_FAIL and error_rate >= request_error_rate_threshold:
                        error_msg = (
                            f"Error rate is more than {request_error_rate_threshold:.0%} -- something is broken!"
                        )
                        raise Exception(error_msg) from e

                response[self.prompt_idx_key] = prompt_idx
                output_collector.append(response)

                # Sleep between consecutive requests to avoid rate limit
                await asyncio.sleep(api_call_delay_seconds)

            except IndexError:  # when the queue is empty, the worker is done
                # wait for logging tasks to finish
                await asyncio.gather(*logger_tasks)
                return

    async def request_api(
        self,
        session: RetryClient,
        request_data: dict,
    ) -> dict:
        """
        Request the model with a body of data.

        :param session: HTTPS Session for invoking the endpoint.
        :type session: RetryClient
        :param request_data: Prompt dictionary to query the model with. (Pass {"prompt": prompt} instead of prompt.)
        :type request_data: dict
        :return: Response from the model.
        :rtype: dict
        """

        self._log_request(request_data)

        token = await self.token_manager.get_token()

        headers = {
            "Content-Type": "application/json",
            "X-CV": f"{uuid.uuid4()}",
            "X-ModelType": self.model or "",
        }

        if self.token_manager.auth_header == "Bearer":
            headers["Authorization"] = f"Bearer {token}"
        elif self.token_manager.auth_header == "api-key":
            headers["api-key"] = token
            headers["Authorization"] = "api-key"

        # Update timeout for proxy endpoint
        if self.azureml_model_deployment:
            headers["azureml-model-deployment"] = self.azureml_model_deployment

        # add all additional headers
        if self.additional_headers:
            headers.update(self.additional_headers)

        params = {}
        if self.api_version:
            params["api-version"] = self.api_version

        time_start = time.time()
        full_response = None
        async with session.post(url=self.endpoint_url, headers=headers, json=request_data, params=params) as response:
            if response.status == 200:
                response_data = await response.json()
                self.logger.info("Response: %s", response_data)

                # Copy the full response and return it to be saved in jsonl.
                full_response = copy.copy(response_data)

                time_taken = time.time() - time_start

                parsed_response = self._parse_response(response_data)
            else:
                raise HTTPException(
                    reason="Received unexpected HTTP status: {} {}".format(response.status, await response.text())
                )

        return {
            "request": request_data,
            "response": parsed_response,
            "time_taken": time_taken,
            "full_response": full_response,
        }

    def _parse_response(self, response_data: dict) -> dict:
        # https://platform.openai.com/docs/api-reference/completions
        samples = []
        finish_reason = []
        for choice in response_data["choices"]:
            if "text" in choice:
                samples.append(choice["text"])
            if "finish_reason" in choice:
                finish_reason.append(choice["finish_reason"])

        return {"samples": samples, "finish_reason": finish_reason, "id": response_data["id"]}


# ===========================================================
# ============== OpenAIChatCompletionsModel =================
# ===========================================================


class OpenAIChatCompletionsModel(OpenAICompletionsModel):
    """
    OpenAIChatCompletionsModel is a wrapper around OpenAICompletionsModel that
    formats the prompt for chat completion.
    """
    # pylint: disable=keyword-arg-before-vararg
    def __init__(self, name="OpenAIChatCompletionsModel", *args, **kwargs):
        super().__init__(name=name, *args, **kwargs)

    def format_request_data(self, prompt: List[dict], **request_params):  # type: ignore[override]
        # Caption images if available
        if len(self.image_captions.keys()):
            for message in prompt:
                message["content"] = replace_prompt_captions(
                    message["content"],
                    captions=self.image_captions,
                )

        request_data = {"messages": prompt, **self.get_model_params()}
        request_data.update(request_params)
        return request_data

    async def get_conversation_completion(
        self,
        messages: List[dict],
        session: RetryClient,
        role: str = "assistant",
        **request_params,
    ) -> dict:
        """
        Query the model a single time with a message.

        :param messages: List of messages to query the model with.
                         Expected format: [{"role": "user", "content": "Hello!"}, ...]
        :type messages: List[dict]
        :param session: aiohttp RetryClient object to query the model with.
        :type session: RetryClient
        :param role: Not used for this model, since it is a chat model.
        :type role: str
        :keyword **request_params: Additional parameters to pass to the model.
        :return: Dictionary containing the completion response.
        :rtype: dict
        """
        request_data = self.format_request_data(
            messages=messages,
            **request_params,
        )
        return await self.request_api(
            session=session,
            request_data=request_data,
        )

    async def get_completion(
        self,
        prompt: str,
        session: RetryClient,
        **request_params,
    ) -> dict:
        """
        Query a ChatCompletions model with a single prompt.

        :param prompt: Prompt str to query model with.
        :type prompt: str
        :param session: aiohttp RetryClient object to use for the request.
        :type session: RetryClient
        :keyword **request_params: Additional parameters to pass to the request.
        :return: Dictionary containing the completion response.
        :rtype: dict
        """
        messages = [{"role": "system", "content": prompt}]

        request_data = self.format_request_data(messages=messages, **request_params)
        return await self.request_api(
            session=session,
            request_data=request_data,
        )

    async def get_all_completions(
        self,
        prompts: List[str],  # type: ignore[override]
        session: RetryClient,
        api_call_max_parallel_count: int = 1,
        api_call_delay_seconds: float = 0.1,
        request_error_rate_threshold: float = 0.5,
        **request_params,
    ) -> List[dict]:
        prompts_list = [{"role": "system", "content": prompt} for prompt in prompts]

        return await super().get_all_completions(
            prompts=prompts_list,
            session=session,
            api_call_max_parallel_count=api_call_max_parallel_count,
            api_call_delay_seconds=api_call_delay_seconds,
            request_error_rate_threshold=request_error_rate_threshold,
            **request_params,
        )

    def _parse_response(self, response_data: dict) -> dict:
        # https://platform.openai.com/docs/api-reference/chat
        samples = []
        finish_reason = []

        for choice in response_data["choices"]:
            if "message" in choice and "content" in choice["message"]:
                samples.append(choice["message"]["content"])
            if "message" in choice and "finish_reason" in choice["message"]:
                finish_reason.append(choice["message"]["finish_reason"])

        return {"samples": samples, "finish_reason": finish_reason, "id": response_data["id"]}


# ===========================================================
# =========== OpenAIMultiModalCompletionsModel ==============
# ===========================================================


class OpenAIMultiModalCompletionsModel(OpenAICompletionsModel):
    """
    Wrapper around OpenAICompletionsModel that formats the prompt for multimodal
    completions containing images.
    """

    model_param_names = ["temperature", "max_tokens", "top_p", "n", "stop"]
    # pylint: disable=keyword-arg-before-vararg
    def __init__(self, name="OpenAIMultiModalCompletionsModel", images_dir: Optional[str] = None, *args, **kwargs):
        self.images_dir = images_dir

        super().__init__(name=name, *args, **kwargs)

    def format_request_data(self, prompt: str, **request_params) -> dict:
        # Replace images if available
        transcript = format_multimodal_prompt(
            prompt=prompt,
            images_dir=self.images_dir,
            captions=self.image_captions,
        )
        request = {"transcript": transcript, **self.get_model_params()}
        request.update(request_params)
        return request

    def _log_request(self, request: dict) -> None:
        """
        Log prompt, ignoring image data if multimodal.

        :param request: The request dictionary.
        :type request: dict
        """
        loggable_prompt_transcript = {
            "transcript": [
                (c if c["type"] != "image" else {"type": "image", "data": "..."}) for c in request["transcript"]
            ],
            **{k: v for k, v in request.items() if k != "transcript"},
        }
        super()._log_request(loggable_prompt_transcript)


# ===========================================================
# ============== LLAMA CompletionsModel =====================
# ===========================================================


class LLAMACompletionsModel(OpenAICompletionsModel):
    """
    Object for calling a Completions-style API for LLAMA models.
    """
    # pylint: disable=keyword-arg-before-vararg
    def __init__(self, name: str = "LLAMACompletionsModel", *args, **kwargs):
        super().__init__(name=name, *args, **kwargs)
        # set authentication header to Bearer, as llama apis always uses the bearer auth_header
        self.token_manager.auth_header = "Bearer"

    def format_request_data(self, prompt: str, **request_params):
        """
        Format the request data for the OpenAI API.

        :param prompt: The prompt string.
        :type prompt: str
        :keyword request_params: Additional request parameters.
        :return: The formatted request data.
        :rtype: dict
        """
        # Caption images if available
        if len(self.image_captions.keys()):
            prompt = replace_prompt_captions(
                prompt=prompt,
                captions=self.image_captions,
            )

        request_data = {
            "input_data": {
                "input_string": [prompt],
                "parameters": {"temperature": self.temperature, "max_gen_len": self.max_tokens},
            }
        }

        request_data.update(request_params)
        return request_data

    # pylint: disable=arguments-differ
    def _parse_response(self, response_data: dict, request_data: dict) -> dict:  # type: ignore[override]
        prompt = request_data["input_data"]["input_string"][0]

        # remove prompt text from each response as llama model returns prompt + completion instead of only completion
        # remove any text after the stop tokens, since llama doesn't support stop token
        for idx, _ in enumerate(response_data["samples"]):
            response_data["samples"][idx] = response_data["samples"][idx].replace(prompt, "").strip()
            for stop_token in self.stop:
                if stop_token in response_data["samples"][idx]:
                    response_data["samples"][idx] = response_data["samples"][idx].split(stop_token)[0].strip()

        samples = []
        finish_reason = []
        for choice in response_data:
            if "0" in choice:
                samples.append(choice["0"])
                finish_reason.append("Stop")

        return {
            "samples": samples,
            "finish_reason": finish_reason,
        }


# ===========================================================
# ============== LLAMA ChatCompletionsModel =================
# ===========================================================
class LLAMAChatCompletionsModel(LLAMACompletionsModel):
    """
    LLaMa ChatCompletionsModel is a wrapper around LLaMaCompletionsModel that
    formats the prompt for chat completion.
    This chat completion model should be only used as assistant,
    and shouldn't be used to simulate user. It is not possible
    to pass a system prompt do describe how the model would behave,
    So we only use the model as assistant to reply for questions made by GPT simulated users.
    """
    # pylint: disable=keyword-arg-before-vararg
    def __init__(self, name="LLAMAChatCompletionsModel", *args, **kwargs):
        super().__init__(name=name, *args, **kwargs)
        # set authentication header to Bearer, as llama apis always uses the bearer auth_header
        self.token_manager.auth_header = "Bearer"

    def format_request_data(self, prompt: List[dict], **request_params):  # type: ignore[override]
        # Caption images if available
        if len(self.image_captions.keys()):
            for message in prompt:
                message["content"] = replace_prompt_captions(
                    message["content"],
                    captions=self.image_captions,
                )

        # For LLaMa we don't pass the prompt (user persona) as a system message
        # since LLama doesn't support system message
        # LLama only supports user, and assistant messages.
        # The messages sequence has to start with User message/ It can't have two user or
        # two assistant consecutive messages.
        # so if we set the system meta prompt as a user message,
        # and if we have the first two messages made by user then we
        # combine the two messages in one message.
        for _, x in enumerate(prompt):
            if x["role"] == "system":
                x["role"] = "user"
        if len(prompt) > 1 and prompt[0]["role"] == "user" and prompt[1]["role"] == "user":
            prompt[0] = {"role": "user", "content": prompt[0]["content"] + "\n" + prompt[1]["content"]}
            del prompt[1]

        # request_data = {"messages": messages, **self.get_model_params()}
        request_data = {
            "input_data": {
                "input_string": prompt,
                "parameters": {"temperature": self.temperature, "max_new_tokens": self.max_tokens},
            },
        }
        request_data.update(request_params)
        return request_data

    async def get_conversation_completion(
        self,
        messages: List[dict],
        session: RetryClient,
        role: str = "assistant",
        **request_params,
    ) -> dict:
        """
        Query the model a single time with a message.

        :param messages: List of messages to query the model with.
                         Expected format: [{"role": "user", "content": "Hello!"}, ...]
        :type messages: List[dict]
        :param session: aiohttp RetryClient object to query the model with.
        :type session: RetryClient
        :param role: Not used for this model, since it is a chat model.
        :type role: str
        :keyword request_params: Additional parameters to pass to the model.
        :return: Dictionary containing the response from the model.
        :rtype: dict
        """

        request_data = self.format_request_data(
            messages=messages,
            **request_params,
        )
        return await self.request_api(
            session=session,
            request_data=request_data,
        )

    # pylint: disable=arguments-differ
    def _parse_response(self, response_data: dict) -> dict:  # type: ignore[override]
        # https://platform.openai.com/docs/api-reference/chat
        samples = []
        finish_reason = []
        # for choice in response_data:
        if "output" in response_data:
            samples.append(response_data["output"])
            finish_reason.append("Stop")

        return {
            "samples": samples,
            "finish_reason": finish_reason,
            # "id": response_data["id"]
        }
