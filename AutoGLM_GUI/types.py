from __future__ import annotations

from enum import Enum
from typing import Literal

from typing_extensions import TypedDict

ActionMetadata = Literal["do", "finish", "takeover"]
PhoneActionType = Literal[
    "Tap", "Swipe", "Type", "Launch", "Wait", "Back", "Home", "Long Press", "Double Tap"
]
MessageRole = Literal["system", "user", "assistant"]
ContentType = Literal["text", "image_url"]


class PhoneAgentAction(TypedDict, total=False):
    _metadata: ActionMetadata
    action: PhoneActionType
    element: list[int]
    text: str
    app: str
    start: list[int]
    end: list[int]
    duration: str
    message: str


class SSEThinkingChunkData(TypedDict):
    type: str
    role: str
    chunk: str


class SSEStepData(TypedDict, total=False):
    type: str
    role: str
    step: int
    thinking: str
    action: PhoneAgentAction | None
    success: bool
    finished: bool


class SSEDoneData(TypedDict, total=False):
    type: str
    role: str
    message: str
    steps: int
    success: bool


class SSEErrorData(TypedDict):
    type: str
    role: str
    message: str


SSEEventData = SSEThinkingChunkData | SSEStepData | SSEDoneData | SSEErrorData


class AgentSpecificConfig(TypedDict, total=False):
    """Reserved agent options; built-in executors currently need no extra fields."""


SUPPORTED_AGENT_TYPES = frozenset(
    {"glm-async", "async-glm", "qwen", "gemini", "general-vision"}
)
REMOVED_AGENT_TYPES = frozenset({"mai", "droidrun", "midscene"})


def normalize_agent_type(agent_type: str) -> str:
    """Keep the original GLM name readable while using its current executor."""
    return "glm-async" if agent_type == "glm" else agent_type


class TextContent(TypedDict):
    type: ContentType
    text: str


class ImageURLContent(TypedDict):
    type: ContentType
    image_url: dict[str, str]


MessageContent = str | list[TextContent | ImageURLContent]


class ChatMessage(TypedDict, total=False):
    role: MessageRole
    content: MessageContent


ConversationContext = list[ChatMessage]


class DeviceConnectionType(Enum):
    """Device connection type.

    Unified enum for device connection types to avoid confusion
    with phone_agent's ConnectionType.

    - USB: Physical USB connection
    - WIFI: ADB over WiFi (local network)
    - REMOTE: HTTP Remote Device (via Device Agent Server)
    """

    USB = "usb"
    WIFI = "wifi"
    REMOTE = "remote"
    RMQ1 = "rmq1"
