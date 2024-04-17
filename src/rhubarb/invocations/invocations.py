# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import re
import json
import time
import random
import logging
from typing import Any, List, Optional, Generator
from contextlib import contextmanager

from jsonschema import ValidationError, validate

from rhubarb.config import GlobalConfig
from rhubarb.models import EmbeddingModels

logger = logging.getLogger(__name__)

@contextmanager
def retry_with_backoff(bedrock_client, max_retries, initial_backoff):
    """
    Exponential back-off with jitter context manager for Bedrock API
    calls.
    """
    retries = 0
    backoff = initial_backoff
    while True:
        try:
            yield
        except bedrock_client.exceptions.ThrottlingException as e:
            if retries == max_retries:
                raise e
            sleep_time = backoff + random.uniform(0, backoff)
            time.sleep(sleep_time)
            backoff *= 2
            retries += 1
        except Exception as e:
            logger.error(f"An unexpected error occurred: {str(e)}")
            raise e
        else:
            break


class Invocations:
    """
    A class to invoke Bedrock models and handle retries and exponential backoff.

    Args:
        body (Any): The input data for the model inference.
        bedrock_client (Any): A boto3 client instance for the Bedrock Runtime service.
        model_id (str): The identifier of the Bedrock model to be invoked.

    Methods:
        invoke_model_stream(): Invokes the specified Bedrock model for streaming inference
            and yields the output in text chunks.
        invoke_model_json(): Invokes the specified Bedrock model for inference and returns
            the JSON response as a dictionary.
        invoke_embedding(): Invokes the specified Bedrock embedding model and returns embeddings
    """
    def __init__(
        self,
        body: Any,
        bedrock_client: Any,
        model_id: str,
        output_schema: Optional[Any] = None,
    ):
        
        self.body = body
        self.bedrock_client = bedrock_client
        self.model_id = model_id
        self.output_schema = output_schema
        self.config = GlobalConfig.get_instance()
        self.reprompt_count = self.config.retry_for_incomplete_json
        self.history = None
        self.token_usage = None

    def _reprompt_for_proper_json(self) -> None:
        self.reprompt_count = self.reprompt_count - 1
        messages = self.body["messages"]
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"""The response generated by you is either incomplete or is an invalid JSON that doesn't conform with the provided schema. 
                            - Analyze the document page(s), think step-by-step, and retry extracting the values specified in the JSON schema.
                            - Make sure to respond only with a valid JSON wrapped in three backticks.                             
                            <schema>
                            {json.dumps(self.output_schema)}
                            </schema>
                            """
                    }
                ],
            }
        )
        self.body["messages"] = messages
        response = self.invoke_model_json()
        return response["output"]

    def _validate_output_schema(self, data: dict, response_text: str) -> dict:
        try:
            if self.output_schema:
                # validate json_data here
                if "output_schema" in self.output_schema:
                    validate(instance=data, schema=self.output_schema["output_schema"])
                else:
                    validate(instance=data, schema=self.output_schema)
        except ValidationError:
            # if json invalid then retry with model
            if self.reprompt_count > 0:
                return self._reprompt_for_proper_json()
            else:
                return response_text
        else:
            return data

    def _extract_json_from_markdown(self, response_text):
        json_block_pattern = r"```(?:json)?\n([\s\S]*?)\n```"
        code_block_pattern = r"```([\s\S]*?)\n```"

        json_blocks = re.findall(json_block_pattern, response_text, re.MULTILINE)
        code_blocks = re.findall(code_block_pattern, response_text, re.MULTILINE)
        json_data = []

        for block in json_blocks:
            try:
                json_data.append(json.loads(block))
            except json.JSONDecodeError:
                pass  # Ignore invalid JSON blocks

        if not json_data:
            for block in code_blocks:
                try:
                    json_data.append(json.loads(block))
                except json.JSONDecodeError:
                    pass  # Ignore non-JSON code blocks
        if json_data:
            # validate json_data with schema
            # if error re-prompt else return json_data
            data = json_data[0]
            if self.output_schema:
                return self._validate_output_schema(
                    data=data, response_text=response_text
                )
            else:
                return data
        else:
            # return response_text
            if self.output_schema:
                # there's schema but json_data was not present
                # indicating malformed or incomplete JSON in response
                if self.reprompt_count > 0:
                    return self._reprompt_for_proper_json()
                else:
                    return response_text
            else:
                return response_text

    @property
    def message_history(self) -> Any:
        return self.history

    def invoke_model_json(self) -> dict:
        """
        Invokes the specified Bedrock model to run inference using the input provided in self.body,
        and returns the JSON response as a dictionary.

        Returns:
            dict: A dictionary containing the following keys:
                - 'output': The JSON output from the model inference. If the output is not a valid JSON,
                            it is returned as a string.
                - 'token_usage': The token usage metrics for the inference.

        Raises:
            Exception: Any exception raised by the boto3 client, except ThrottlingException which is
            handled by retrying. If the maximum number of retries is reached and ThrottlingException
            still occurs, it is raised to the caller.

        Notes:
            - The output from the model inference is assumed to be a JSON string wrapped in quotation marks
            and prefixed with 'OUTPUT: '.
            - If the output is not a valid JSON, it is returned as a string in the 'output' key of the result dictionary.
            - Any other exception is raised to the caller without any retries.
        """
        with retry_with_backoff(
            self.bedrock_client, self.config.max_retries, self.config.initial_backoff
        ):
            response = self.bedrock_client.invoke_model(
                body=json.dumps(self.body),
                contentType="application/json",
                accept="application/json",
                modelId=self.model_id,
            )

        with response["body"] as stream:
            response_body = json.load(stream)

        response_text = response_body["content"][0]["text"]
        self.token_usage = response_body["usage"]

        messages = self.body["messages"]
        messages.append(
            {"role": response_body["role"], "content": response_body["content"]}
        )

        self.history = messages
        output = self._extract_json_from_markdown(response_text)

        final_response = {"output": output, "token_usage": self.token_usage}
        logger.info({"token_usage": self.token_usage})
        return final_response

    def invoke_model_stream(self) -> Generator[Any, Any, Any]:
        """
        Invokes the specified Bedrock model to run streaming inference using the input provided,
        and yields the inference output in text chunks.

        Yields:
            str: Text chunks from the inference output. Special markers '|-START-|\n' and '\n|-END-|'
            are yielded for the start and end of the output, respectively.

        Raises:
            Exception: Any exception raised by the boto3 client, except ThrottlingException which is
            handled by retrying.

        Notes:
            - If the maximum number of retries is reached and ThrottlingException still occurs, it is raised
            to the caller.
            - Any other exception is raised to the caller without any retries.
        """
        with retry_with_backoff(
            self.bedrock_client, self.config.max_retries, self.config.initial_backoff
        ):
            response = self.bedrock_client.invoke_model_with_response_stream(
                body=json.dumps(self.body),
                contentType="application/json",
                accept="application/json",
                modelId=self.model_id,
            )
            full_response_text = ""
            for event in response["body"]:
                if "chunk" in event:
                    chunk = event["chunk"]["bytes"]
                    data = json.loads(chunk.decode("utf-8"))
                    if data["type"] == "content_block_start":
                        yield "|-START-|\n"
                    elif data["type"] == "content_block_delta":
                        full_response_text = full_response_text + data["delta"]["text"]
                        yield data["delta"]["text"]
                    elif data["type"] == "content_block_stop":
                        yield "\n|-END-|"
                    elif data["type"] == "message_stop":
                        metrics = data["amazon-bedrock-invocationMetrics"]
                        token_count = {
                            "input_tokens": metrics["inputTokenCount"],
                            "output_tokens": metrics["outputTokenCount"],
                        }
                        logger.info(token_count)
                        yield token_count
            messages = self.body["messages"]
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": full_response_text}],
                }
            )
            self.history = messages

    def invoke_embedding(self) -> List[Any]:
        """
        Invokes the specified Bedrock embedding model.

        Returns:
            list: a single vector embedding or a list of vector embeddings

        Raises:
            Exception: Any exception raised by the boto3 client, except ThrottlingException which is
            handled by retrying.

        Notes:
            - If the maximum number of retries is reached and ThrottlingException still occurs, it is raised
            to the caller.
            - Any other exception is raised to the caller without any retries.
            - Titan Embedding models return a single vector embedding.
            - Cohere Emebedding models return a list of vector embeddings.
        """
        with retry_with_backoff(
            self.bedrock_client, self.config.max_retries, self.config.initial_backoff
        ):
            response = self.bedrock_client.invoke_model(
                body=json.dumps(self.body),
                contentType="application/json",
                accept="application/json",
                modelId=self.model_id,
            )
        with response["body"] as stream:
            response_body = json.load(stream)

        if self.model_id == EmbeddingModels.TITAN_EMBED_MM_V1.value:
            return response_body['embedding']
        else:
            return response_body

        