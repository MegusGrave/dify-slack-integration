import json
import traceback
from typing import Mapping
from werkzeug import Request, Response
from dify_plugin import Endpoint
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


class SlackEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        """
        Invokes the endpoint with the given request.
        """
        # Check if this is a retry and if we should ignore it
        retry_num = r.headers.get("X-Slack-Retry-Num")
        if (not settings.get("allow_retry") and (r.headers.get("X-Slack-Retry-Reason") == "http_timeout" or 
                                                ((retry_num is not None and int(retry_num) > 0)))):
            return Response(status=200, response="ok")
        
        # Parse the incoming JSON data
        data = r.get_json()

        # Handle Slack URL verification challenge
        if data.get("type") == "url_verification":
            return Response(
                response=json.dumps({"challenge": data.get("challenge")}),
                status=200,
                content_type="application/json"
            )
        
        # Handle Slack events
        if (data.get("type") == "event_callback"):
            event = data.get("event")
            
            # Handle different event types
            if event.get("type") == "app_mention":
                # Handle mention events - when the bot is @mentioned
                return self._handle_mention(event, settings)
            elif event.get("type") == "message":
                # Handle direct messages and channel messages
                return self._handle_message(event, settings)
            else:
                # Other event types we're not handling
                return Response(status=200, response="ok")
        else:
            # Not an event we're handling
            return Response(status=200, response="ok")
    
    def _handle_mention(self, event, settings):
        """
        Handle when the bot is mentioned in a channel
        """
        message = event.get("text", "")
        # Remove the bot mention from the beginning of the message
        if message.startswith("<@"):
            message = message.split("> ", 1)[1] if "> " in message else message
            
        # Get channel ID and thread timestamp
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts", event.get("ts"))
        
        # Process the message and respond
        return self._process_and_respond(message, channel, thread_ts, settings)
    
    def _handle_message(self, event, settings):
        """
        Handle direct messages or channel messages
        """
        # Ignore messages from bots to prevent loops
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return Response(status=200, response="ok")
        
        # Only process direct messages (DMs) or messages in threads where the bot was previously mentioned
        # This prevents the bot from responding to every message in channels
        channel = event.get("channel", "")
        is_dm = channel.startswith("D")  # DM channels start with D in Slack
        thread_ts = event.get("thread_ts")
        
        # If not in a DM and not in a thread, ignore the message
        if not is_dm and not thread_ts:
            return Response(status=200, response="ok")
            
        message = event.get("text", "")
        thread_ts = thread_ts if thread_ts else event.get("ts")
        
        # Process the message and respond
        return self._process_and_respond(message, channel, thread_ts, settings)
    
    def _process_and_respond(self, message, channel, thread_ts, settings):
        """
        Process the message with Dify and respond in Slack
        """
        token = settings.get("bot_token")
        client = WebClient(token=token)
        
        try:
            # Get thread history for better context if in a thread
            thread_history = []
            if thread_ts:
                thread_history = self._get_thread_history(client, channel, thread_ts)
            
            # Create a consistent conversation ID based on channel and thread
            # This ensures context is maintained within the same thread
            conversation_id = f"slack-{channel}-{thread_ts}"
            
            # Invoke the Dify app with the message
            response = self.session.app.chat.invoke(
                app_id=settings["app"]["app_id"],
                query=message,
                inputs={},
                conversation_id=conversation_id,  # Use consistent ID for context
                response_mode="blocking",
                user="slack-user"
            )
            
            # Send the response back to Slack
            try:
                result = client.chat_postMessage(
                    channel=channel,
                    text=response.get("answer"),
                    thread_ts=thread_ts  # Reply in thread
                )
                return Response(status=200, response="ok")
            except SlackApiError as e:
                print(f"Error sending message to Slack: {e}")
                return Response(
                    status=200,
                    response=f"Error sending message to Slack: {str(e)}",
                    content_type="text/plain"
                )
        except Exception as e:
            err = traceback.format_exc()
            print(f"Error processing message: {err}")
            
            # Send error message to Slack
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="Sorry, I'm having trouble processing your request. Please try again later."
                )
            except SlackApiError:
                # Failed to send error message
                pass
                
            return Response(
                status=200,  # Always return 200 to Slack to acknowledge receipt
                response=f"An error occurred: {str(e)}",
                content_type="text/plain"
            )
    
    def _get_thread_history(self, client, channel, thread_ts):
        """
        Get the conversation history from a thread
        """
        try:
            result = client.conversations_replies(
                channel=channel,
                ts=thread_ts
            )
            
            # Format messages for context
            history = []
            for msg in result.get("messages", []):
                # Skip the original message we're currently replying to
                if msg.get("ts") == thread_ts:
                    continue
                    
                role = "assistant" if msg.get("bot_id") else "user"
                history.append({
                    "role": role,
                    "content": msg.get("text", "")
                })
            
            return history
        except SlackApiError as e:
            print(f"Error getting thread history: {e}")
            return []
