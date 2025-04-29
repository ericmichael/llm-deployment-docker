"""This module contains the Agent class.

The Agent class is responsible for interacting with the user.
Typical usage example:

    agent = Agent()
    response = agent.chat("Tell me a joke")
"""

import os
import json
from openai import OpenAI, AzureOpenAI
import re
from django.conf import settings
from ..models import Message

# Initialize the OpenAI client
if settings.OPENAI_API_TYPE == "azure":
    client = AzureOpenAI(
        api_version=settings.OPENAI_API_VERSION,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    )
else:
    client = OpenAI(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_API_BASE
    )


class Agent:
    """A class used to interact with the user and invoke the necessary tools.

    Attributes:
        tools: A dictionary of tools where the key is the tool's name and the
               value is a dictionary containing the tool's parameters and function.
        tool_invoker: An instance of the ToolInvoker class.
        history: A list of previous interactions with the user.
        prompt: A string used as the initial prompt for the chat.
    """

    def __init__(self, prompt="You are a helpful assistant.", thread=None) -> None:
        self.thread = thread
        self.history = self._build_history()
        self.prompt = prompt

    def chat(self, message):
        """Interacts with the user and invokes the necessary tools.

        Args:
            message: A string containing the user's input.

        Returns:
            A string containing the assistant's response.
        """
        ai_reply = self._get_ai_reply(message, system_message=self.prompt.strip())
        self._update_history("user", message)
        self._update_history("assistant", ai_reply)

        return ai_reply

    def _build_history(self):
        """Builds the history from the thread messages.

        Returns:
            A list of previous interactions with the user.
        """
        history = []
        if self.thread is not None:  # Ensure that thread is not None
            messages = Message.objects.filter(thread=self.thread).order_by("timestamp")
            for message in messages:
                history.append({"role": message.role, "content": message.content})
        return history

    def _get_ai_reply(
        self, message, model="gpt-35-turbo-16k", system_message=None, temperature=0
    ):
        """Gets a response from the AI model.

        Args:
            message: A string containing the user's input.
            model: A string containing the name of the AI model.
            system_message: A string containing a system message.
            temperature: A float used to control the randomness of the AI's output.

        Returns:
            A string containing the AI's response.
        """
        messages = self._prepare_messages(message, system_message)
        completion = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature
        )
        return completion.choices[0].message.content.strip()

    def _prepare_messages(self, message, system_message):
        """Prepares the messages for the AI model.

        Args:
            message: A string containing the user's input.
            system_message: A string containing a system message.

        Returns:
            A list of messages for the AI model.
        """
        messages = []
        if system_message is not None:
            messages.append({"role": "system", "content": system_message})
        messages.extend(self.history)
        if message is not None:
            messages.append({"role": "user", "content": message})
        return messages

    def _update_history(self, role, content):
        """Updates the history of interactions with the user.

        Args:
            role: A string indicating the role of the message sender.
            content: A string containing the message content.
        """
        self.history.append({"role": role, "content": content})
        # Create and save a Message instance
        if self.thread is not None:  # Ensure that thread is not None
            Message.objects.create(
                thread=self.thread, user=self.thread.user, content=content, role=role
            )

    def generate_title(self, conversation):
        """Generates a title for the conversation.

        Args:
            conversation: The first exchange between user and assistant.

        Returns:
            A string containing the generated title.
        """
        prompt = """Based on the conversation below, generate a concise title (10 words or less) that captures the main topic.
        Keep it simple and descriptive. Just return the title, nothing else.

        Conversation:
        {conversation}
        """
        
        messages = [
            {"role": "system", "content": prompt.format(conversation=conversation)},
        ]
        
        try:
            completion = client.chat.completions.create(
                model=self.thread.model if self.thread else "gpt-3.5-turbo",
                messages=messages,
                temperature=0,
                max_tokens=60
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            return "New Chat"
