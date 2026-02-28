import chainlit as cl
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph_checkpoint_firestore import FirestoreSaver
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import TypedDict, Sequence, Annotated
import operator
import os
from dotenv import load_dotenv
from google.cloud.firestore import SERVER_TIMESTAMP

# Load environment variables
load_dotenv()


# Custom FirestoreSaver that adds timestamps for TTL policies
class TimestampedFirestoreSaver(FirestoreSaver):
    """Extends FirestoreSaver to add server timestamps for Firestore TTL policies."""
    
    def put(self, config, checkpoint, metadata, new_versions):
        """Override put to add timestamp field to checkpoint documents."""
        result = super().put(config, checkpoint, metadata, new_versions)
        
        # Add timestamps to BOTH partition and checkpoint documents for complete TTL cleanup
        # Structure: /checkpoints/{thread_id}_{checkpoint_ns}/checkpoints/{checkpoint_id}
        if result:
            thread_id = result.get("configurable", {}).get("thread_id")
            checkpoint_ns = result.get("configurable", {}).get("checkpoint_ns", "")
            checkpoint_id = result.get("configurable", {}).get("checkpoint_id")
            
            if thread_id and checkpoint_id:
                # Add timestamp to partition document (session/thread container)
                partition_doc_ref = self.checkpoints_collection.document(f"{thread_id}_{checkpoint_ns}")
                partition_doc_ref.set({"created_at": SERVER_TIMESTAMP}, merge=True)
                
                # Add timestamp to checkpoint document (actual checkpoint data)
                checkpoint_doc_ref = partition_doc_ref.collection("checkpoints").document(checkpoint_id)
                checkpoint_doc_ref.set({"created_at": SERVER_TIMESTAMP}, merge=True)
        
        return result
    
    async def aput(self, config, checkpoint, metadata, new_versions):
        """Override aput to add timestamp field to checkpoint documents (async version)."""
        result = await super().aput(config, checkpoint, metadata, new_versions)
        
        # Add timestamps to BOTH partition and checkpoint documents for complete TTL cleanup
        if result:
            thread_id = result.get("configurable", {}).get("thread_id")
            checkpoint_ns = result.get("configurable", {}).get("checkpoint_ns", "")
            checkpoint_id = result.get("configurable", {}).get("checkpoint_id")
            
            if thread_id and checkpoint_id:
                # Add timestamp to partition document (session/thread container)
                partition_doc_ref = self.checkpoints_collection.document(f"{thread_id}_{checkpoint_ns}")
                partition_doc_ref.set({"created_at": SERVER_TIMESTAMP}, merge=True)
                
                # Add timestamp to checkpoint document (actual checkpoint data)
                checkpoint_doc_ref = partition_doc_ref.collection("checkpoints").document(checkpoint_id)
                checkpoint_doc_ref.set({"created_at": SERVER_TIMESTAMP}, merge=True)
        
        return result


# Initialize the model
# Using gemini-3-flash-preview as per user request (and verified existence)
# Provide your API key in the .env file
model = ChatGoogleGenerativeAI(model="gemini-3-flash-preview", google_api_key=os.getenv("GOOGLE_API_KEY"))

# Define the state
class AgentState(TypedDict):
    messages: Annotated[Sequence[HumanMessage | AIMessage | SystemMessage], operator.add]

# Define the node function that calls the model
async def call_model(state: AgentState):
    messages = state["messages"]
    response = await model.ainvoke(messages)
    return {"messages": [response]}

# Build the graph
builder = StateGraph(AgentState)
builder.add_node("model", call_model)
builder.add_edge(START, "model")
builder.add_edge("model", END)

# Add Firestore-based memory to persist state across interactions and restarts
# TimestampedFirestoreSaver adds `created_at` field to each checkpoint for TTL policies
checkpointer = TimestampedFirestoreSaver(project_id="mooballai", checkpoints_collection="checkpoints")
graph = builder.compile(checkpointer=checkpointer)

@cl.on_chat_start
async def start():
    # Generate a session ID for the thread
    import uuid
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)
    await cl.Message(content="Hello! I am connected to Google Gemini. How can I help you today?").send()

@cl.on_message
async def main(message: cl.Message):
    # Use the session ID as the thread ID to maintain conversation history
    thread_id = cl.user_session.get("thread_id")
    config = {"configurable": {"thread_id": thread_id}}
    
    # Run the graph with the new user message
    inputs = {"messages": [HumanMessage(content=message.content)]}
    
    # Invoke the graph and stream the response
    msg = cl.Message(content="")
    await msg.send()
    
    async for event in graph.astream_events(inputs, config=config, version="v1"):
        kind = event["event"]
        if kind == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            if content:
                # Handle list of content parts (e.g. from Gemini experimental models)
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            chunk_text = part.get("text", "")
                            if chunk_text:
                                await msg.stream_token(chunk_text)
                        elif isinstance(part, str):
                            await msg.stream_token(part)
                # Handle single string content
                elif isinstance(content, str):
                    await msg.stream_token(content)
                
    await msg.update()
