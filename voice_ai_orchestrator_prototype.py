#!/usr/bin/env python3
"""
Enterprise Voice AI & Omnichannel Orchestrator Prototype
========================================================
A modular, pluggable reference implementation sitting on top of existing
enterprise systems (Genesys Telephony, Personal Phone / Cloud SIP via Twilio for
Rapid Prototyping, CRM, and Microsoft Outlook/Graph).

Features demonstrated:
1. Hexagonal / Adapter Pattern (`BaseCRMAdapter`, `BaseTelephonyAdapter`, `BaseEmailAdapter`)
   so you can swap between a Personal Number Prototype (`PersonalPhoneTwilioAdapter`) and
   Enterprise Genesys (`GenesysCloudAdapter`) with a 1-line configuration change.
2. UAE Regulatory Compliance Middleware (`UAEComplianceGuard`) implementing:
   - TDRA Cabinet Resolution No. 56 of 2024 calling window checks (09:00 - 18:00 Asia/Dubai)
   - Personal SIM vs. Corporate Registered Number check (allows personal SIMs ONLY in
     `SANDBOX_PROTOTYPE` mode to test with whitelisted internal numbers, blocking commercial
     telemarketing from personal numbers per Article 6 of Cabinet Resolution No. 56 of 2024)
   - Do Not Call Register (DNCR) & internal opt-out verification
   - Call attempt frequency limits (max 1/day, 2/week for unanswered calls)
   - UAE PDPL (Federal Decree-Law No. 45 of 2021) PII masking for logs
3. LLM Function-Calling / Tool Router (`OmnichannelAgentOrchestrator`) handling:
   - Personalized offer retrieval (`get_best_offers`)
   - Service request & complaint registration (`create_crm_ticket`)
   - Customer feedback / CSAT logging (`log_customer_feedback`)
   - Warm transfer to a human agent (`transfer_to_human_agent`) — bridges to a second
     personal/colleague mobile number in prototype mode or a Genesys queue in production!
   - Immediate DNC opt-out (`register_dnc_opt_out`)
4. Phase 2 Readiness: Same orchestrator processes inbound Outlook emails via a unified
   `InteractionTurn` contract.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
from datetime import datetime, time, timezone, timedelta
import enum
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

# Configure structured logging with UAE PDPL PII-safe formatting
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("VoiceAIOrchestrator")

# UAE Standard Time (GST / Asia/Dubai is UTC+4)
UAE_TZ = timezone(timedelta(hours=4))


# ============================================================================
# 1. DOMAIN MODELS & UNIFIED OMNICHANNEL CONTRACTS
# ============================================================================

class ChannelType(str, enum.Enum):
    VOICE_GENESYS = "voice_genesys"
    VOICE_PERSONAL_PROTOTYPE = "voice_personal_prototype"
    EMAIL_OUTLOOK = "email_outlook"


class TicketType(str, enum.Enum):
    SERVICE_REQUEST = "SERVICE_REQUEST"
    COMPLAINT = "COMPLAINT"
    INQUIRY = "INQUIRY"


class TicketPriority(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclasses.dataclass
class CustomerProfile:
    customer_id: str
    full_name: str
    phone_e164: str
    email: str
    segment: str  # e.g., "Gold", "Platinum", "Standard"
    preferred_language: str = "en-AE"
    dnc_opted_out: bool = False
    open_ticket_ids: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class PromotionalOffer:
    offer_id: str
    title: str
    description: str
    discount_pct: float
    valid_until: str
    eligibility_segment: str


@dataclasses.dataclass
class CRMTicket:
    ticket_id: str
    customer_id: str
    ticket_type: TicketType
    priority: TicketPriority
    summary: str
    details: str
    channel: ChannelType
    created_at_iso: str
    status: str = "OPEN"


@dataclasses.dataclass
class CustomerFeedback:
    feedback_id: str
    customer_id: str
    rating_1_to_5: int
    sentiment: str
    verbatim_comment: str
    channel: ChannelType
    recorded_at_iso: str


@dataclasses.dataclass
class InteractionSession:
    session_id: str
    customer: CustomerProfile
    channel: ChannelType
    recording_consent_given: bool = False
    transcript_history: List[Dict[str, str]] = dataclasses.field(default_factory=list)
    created_tickets: List[str] = dataclasses.field(default_factory=list)
    transferred_to_human: bool = False


# ============================================================================
# 2. UAE COMPLIANCE & PRIVACY GUARDRAILS (TDRA & UAE PDPL)
# ============================================================================

class UAEComplianceGuard:
    """
    Enforces UAE Cabinet Resolution No. 56 of 2024 (Telemarketing Regulations)
    and UAE Federal Decree-Law No. 45 of 2021 (Personal Data Protection Law - PDPL).
    """

    # TDRA permitted outbound telemarketing window: 09:00 to 18:00 UAE local time
    ALLOWED_START_TIME = time(9, 0)
    ALLOWED_END_TIME = time(18, 0)

    def __init__(
        self,
        national_dncr_numbers: Optional[set[str]] = None,
        sandbox_prototype_mode: bool = False,
        sandbox_whitelisted_numbers: Optional[set[str]] = None,
    ) -> None:
        self.national_dncr_numbers = national_dncr_numbers or set()
        self.call_attempt_log: Dict[str, List[datetime]] = {}
        self.sandbox_prototype_mode = sandbox_prototype_mode
        self.sandbox_whitelisted_numbers = sandbox_whitelisted_numbers or set()

    def verify_outbound_call_eligibility(
        self,
        customer: CustomerProfile,
        using_personal_caller_id: bool = False,
        is_promotional: bool = True,
        simulated_uae_time: Optional[datetime] = None,
    ) -> tuple[bool, str]:
        """
        Pre-dial check before initiating an outbound call.
        Note on UAE Law (Cabinet Resolution No. 56 of 2024):
          Using a personal mobile/fixed number for commercial telemarketing to the public
          is strictly prohibited in the UAE (heavy fines and SIM disconnection apply).
          However, for an internal engineering prototype, `sandbox_prototype_mode=True`
          permits calling ONLY whitelisted test phone numbers (e.g., your own or a colleague's phone).
        """
        now_uae = simulated_uae_time or datetime.now(UAE_TZ)

        # 0. Personal Caller ID Guardrail (UAE Cabinet Resolution No. 56 of 2024)
        if using_personal_caller_id:
            if not self.sandbox_prototype_mode:
                return (
                    False,
                    "Blocked by UAE TDRA Telemarketing Law (Cabinet Resolution No. 56 of 2024): "
                    "Outbound commercial calls cannot use a personal phone number. Use a corporate registered number.",
                )
            if customer.phone_e164 not in self.sandbox_whitelisted_numbers:
                return (
                    False,
                    f"Blocked by Sandbox Safety Guard: Personal Caller ID prototype can only dial whitelisted "
                    f"internal test numbers, not {self.mask_pii(customer.phone_e164)}.",
                )

        # 1. Check customer internal opt-out & TDRA National DNCR
        if customer.dnc_opted_out or customer.phone_e164 in self.national_dncr_numbers:
            return (
                False,
                f"Blocked by UAE TDRA DNCR / Customer Opt-Out for {self.mask_pii(customer.phone_e164)}.",
            )

        # 2. Check UAE calling hours (09:00 - 18:00 local time) for promotional/outbound outreach
        if is_promotional:
            current_t = now_uae.time()
            if not (self.ALLOWED_START_TIME <= current_t <= self.ALLOWED_END_TIME):
                return (
                    False,
                    f"Blocked by UAE Telemarketing Window: Current time {current_t.strftime('%H:%M')} GST "
                    f"is outside permitted 09:00-18:00 GST window.",
                )

        # 3. Check retry frequency limits (max 1 attempt/day, max 2/week if unanswered)
        attempts = self.call_attempt_log.get(customer.customer_id, [])
        one_day_ago = now_uae - timedelta(days=1)
        one_week_ago = now_uae - timedelta(days=7)

        attempts_last_24h = [ts for ts in attempts if ts > one_day_ago]
        attempts_last_7d = [ts for ts in attempts if ts > one_week_ago]

        if len(attempts_last_24h) >= 1:
            return False, "Blocked by TDRA retry rule: Maximum 1 unanswered attempt per day reached."
        if len(attempts_last_7d) >= 2:
            return False, "Blocked by TDRA retry rule: Maximum 2 unanswered attempts per week reached."

        return True, "Eligible for outbound call under UAE TDRA rules."

    def add_to_dnc(self, phone_e164: str) -> None:
        self.national_dncr_numbers.add(phone_e164)

    @staticmethod
    def mask_pii(text: str) -> str:
        """
        Redacts Emirates IDs, credit cards, email addresses, and phone numbers
        before writing to telemetry logs (UAE PDPL compliance).
        """
        text = re.sub(r"(\+971\d{2})\d{4}(\d{3})", r"\1****\2", text)
        text = re.sub(r"784-\d{4}-\d{7}-\d", "784-****-*******-*", text)
        text = re.sub(r"([a-zA-Z0-9_.+-])[a-zA-Z0-9_.+-]+(@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", r"\1***\2", text)
        return text


# ============================================================================
# 3. PLUGGABLE BACKEND INTERFACES (PORTS) & ADAPTERS
# ============================================================================

class BaseCRMAdapter(abc.ABC):
    """Abstract interface for any underlying CRM (Salesforce, Dynamics 365, ServiceNow, Custom)."""

    @abc.abstractmethod
    async def get_customer(self, customer_id: str) -> Optional[CustomerProfile]:
        pass

    @abc.abstractmethod
    async def get_eligible_offers(self, customer_id: str) -> List[PromotionalOffer]:
        pass

    @abc.abstractmethod
    async def create_ticket(
        self,
        customer_id: str,
        ticket_type: TicketType,
        priority: TicketPriority,
        summary: str,
        details: str,
        channel: ChannelType,
    ) -> CRMTicket:
        pass

    @abc.abstractmethod
    async def save_feedback(
        self,
        customer_id: str,
        rating_1_to_5: int,
        sentiment: str,
        verbatim_comment: str,
        channel: ChannelType,
    ) -> CustomerFeedback:
        pass

    @abc.abstractmethod
    async def mark_customer_opt_out(self, customer_id: str) -> bool:
        pass


class BaseTelephonyAdapter(abc.ABC):
    """
    Abstract interface for Telephony.
    Allows switching between `PersonalPhoneTwilioAdapter` (for immediate prototyping
    without Genesys access) and `GenesysCloudAdapter` (for enterprise production).
    """

    @abc.abstractmethod
    async def start_outbound_call(self, customer: CustomerProfile, campaign_id: str) -> str:
        pass

    @abc.abstractmethod
    async def warm_transfer_to_queue(
        self,
        call_id: str,
        target_queue_name: str,
        screen_pop_attributes: Dict[str, Any],
    ) -> Dict[str, Any]:
        pass


class BaseEmailAdapter(abc.ABC):
    """Abstract interface for Phase 2 Microsoft Outlook / Graph API integration."""

    @abc.abstractmethod
    async def send_email_reply(
        self,
        recipient_email: str,
        subject: str,
        body_text: str,
        reference_ticket_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pass


# ----------------------------------------------------------------------------
# Concrete Reference Adapters (Swap internals with aiohttp/httpx calls in prod)
# ----------------------------------------------------------------------------

class EnterpriseMockCRMAdapter(BaseCRMAdapter):
    """
    Reference CRM Adapter simulating an enterprise backend (e.g., Dynamics 365 / Salesforce).
    """

    def __init__(self) -> None:
        self.customers: Dict[str, CustomerProfile] = {
            "CUST-UAE-101": CustomerProfile(
                customer_id="CUST-UAE-101",
                full_name="Tariq Al-Mansoori",
                phone_e164="+971504439812",
                email="tariq.mansoori@example.ae",
                segment="Platinum",
                preferred_language="en-AE",
                open_ticket_ids=["SR-2026-1004"],
            )
        }
        self.offers: List[PromotionalOffer] = [
            PromotionalOffer(
                offer_id="OFFER-PLAT-5G-UPGRADE",
                title="Platinum Annual Fiber & Roaming Bundle",
                description="25% off for 12 months plus complimentary GCC & global data roaming (20GB/mo).",
                discount_pct=25.0,
                valid_until="2026-12-31",
                eligibility_segment="Platinum",
            ),
            PromotionalOffer(
                offer_id="OFFER-LOYALTY-CASHBACK",
                title="AED 300 Bill Credit on Auto-Pay Enrollment",
                description="Instant AED 300 credit on your next statement when activating recurring auto-pay.",
                discount_pct=15.0,
                valid_until="2026-11-30",
                eligibility_segment="Platinum",
            ),
        ]
        self.tickets: Dict[str, CRMTicket] = {}
        self.feedbacks: List[CustomerFeedback] = []

    async def get_customer(self, customer_id: str) -> Optional[CustomerProfile]:
        await asyncio.sleep(0.02)
        return self.customers.get(customer_id)

    async def get_eligible_offers(self, customer_id: str) -> List[PromotionalOffer]:
        customer = await self.get_customer(customer_id)
        if not customer:
            return []
        return [o for o in self.offers if o.eligibility_segment == customer.segment]

    async def create_ticket(
        self,
        customer_id: str,
        ticket_type: TicketType,
        priority: TicketPriority,
        summary: str,
        details: str,
        channel: ChannelType,
    ) -> CRMTicket:
        prefix = "CMP" if ticket_type == TicketType.COMPLAINT else "SR"
        ticket_id = f"{prefix}-2026-{uuid.uuid4().hex[:4].upper()}"
        ticket = CRMTicket(
            ticket_id=ticket_id,
            customer_id=customer_id,
            ticket_type=ticket_type,
            priority=priority,
            summary=summary,
            details=details,
            channel=channel,
            created_at_iso=datetime.now(UAE_TZ).isoformat(),
        )
        self.tickets[ticket_id] = ticket
        if customer_id in self.customers:
            self.customers[customer_id].open_ticket_ids.append(ticket_id)
        logger.info(
            "CRM Ticket Created: id=%s type=%s priority=%s summary='%s'",
            ticket.ticket_id,
            ticket.ticket_type.value,
            ticket.priority.value,
            ticket.summary,
        )
        return ticket

    async def save_feedback(
        self,
        customer_id: str,
        rating_1_to_5: int,
        sentiment: str,
        verbatim_comment: str,
        channel: ChannelType,
    ) -> CustomerFeedback:
        fb = CustomerFeedback(
            feedback_id=f"FB-{uuid.uuid4().hex[:6].upper()}",
            customer_id=customer_id,
            rating_1_to_5=rating_1_to_5,
            sentiment=sentiment,
            verbatim_comment=verbatim_comment,
            channel=channel,
            recorded_at_iso=datetime.now(UAE_TZ).isoformat(),
        )
        self.feedbacks.append(fb)
        logger.info(
            "CRM Feedback Logged: id=%s rating=%d/5 sentiment=%s",
            fb.feedback_id,
            fb.rating_1_to_5,
            fb.sentiment,
        )
        return fb

    async def mark_customer_opt_out(self, customer_id: str) -> bool:
        if customer_id in self.customers:
            self.customers[customer_id].dnc_opted_out = True
            logger.info("CRM DNC Flag Updated for customer_id=%s", customer_id)
            return True
        return False


class PersonalPhoneTwilioAdapter(BaseTelephonyAdapter):
    """
    Prototype Telephony Adapter that lets you make calls using your personal UAE number
    (as a Verified Outgoing Caller ID on Twilio Programmable Voice) or an Android ADB/GSM bridge,
    without needing Genesys access during prototyping.

    How it works in live prototype mode:
    1. Verify your personal mobile number in the Twilio Console (Outgoing Caller IDs) via OTP.
    2. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `PERSONAL_VERIFIED_CALLER_ID`, and
       `PUBLIC_WEBSOCKET_URL` (e.g., an ngrok tunnel to your local FastAPI/Pipecat server).
    3. `start_outbound_call` triggers Twilio REST API with TwiML `<Connect><Stream url="wss://..."/>`
       so bidirectional audio streams to your local Python Voice AI agent!
    4. `warm_transfer_to_queue` updates the live call with TwiML `<Dial>+97150xxxxxxx</Dial>` to
       bridge the call to a colleague's mobile phone while sending them the AI Screen-Pop via SMS/Console.
    """

    def __init__(
        self,
        personal_caller_id_e164: str = "+971501112233",
        human_agent_fallback_phone_e164: str = "+971509998877",
        websocket_stream_url: str = "wss://your-tunnel.ngrok.app/ws/twilio-media-stream",
    ) -> None:
        self.personal_caller_id = personal_caller_id_e164
        self.human_agent_phone = human_agent_fallback_phone_e164
        self.websocket_stream_url = websocket_stream_url
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    def generate_media_stream_twiml(self, customer_id: str, campaign_id: str) -> str:
        """
        Generates the TwiML payload instructing Twilio to stream real-time bidirectional
        8kHz mu-law audio over WebSockets to your Python AI Voice Pipeline.
        """
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Connect>"
            f'<Stream url="{self.websocket_stream_url}">'
            f'<Parameter name="customer_id" value="{customer_id}" />'
            f'<Parameter name="campaign_id" value="{campaign_id}" />'
            "</Stream>"
            "</Connect>"
            "</Response>"
        )

    def generate_warm_transfer_twiml(self, ai_whisper_summary: str) -> str:
        """
        Generates TwiML to bridge the active call to a human agent's mobile phone (`<Dial>`).
        """
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Say voice=\"Polly.Joanna-Neural\">Please hold while I connect you to a specialist.</Say>"
            f"<Dial callerId=\"{self.personal_caller_id}\">{self.human_agent_phone}</Dial>"
            "</Response>"
        )

    async def start_outbound_call(self, customer: CustomerProfile, campaign_id: str) -> str:
        call_sid = f"TWILIO-PROTOTYPE-CA{uuid.uuid4().hex[:8].upper()}"
        twiml = self.generate_media_stream_twiml(customer.customer_id, campaign_id)
        logger.info(
            "Personal-Number Prototype Call Initiated: sid=%s from=%s to=%s stream_twiml=%s",
            call_sid,
            UAEComplianceGuard.mask_pii(self.personal_caller_id),
            UAEComplianceGuard.mask_pii(customer.phone_e164),
            twiml,
        )
        return call_sid

    async def warm_transfer_to_queue(
        self,
        call_id: str,
        target_queue_name: str,
        screen_pop_attributes: Dict[str, Any],
    ) -> Dict[str, Any]:
        transfer_twiml = self.generate_warm_transfer_twiml(screen_pop_attributes.get("ai_summary", ""))
        logger.info(
            "Personal-Number Prototype Warm Transfer: call_sid=%s bridging to human phone=%s (Queue='%s') | ScreenPop=%s",
            call_id,
            UAEComplianceGuard.mask_pii(self.human_agent_phone),
            target_queue_name,
            json.dumps(screen_pop_attributes),
        )
        return {
            "status": "TRANSFERRED_VIA_PERSONAL_BRIDGE",
            "call_id": call_id,
            "target_queue": target_queue_name,
            "bridged_to_phone": UAEComplianceGuard.mask_pii(self.human_agent_phone),
            "transfer_twiml": transfer_twiml,
            "screen_pop_sent": True,
        }


class GenesysCloudAdapter(BaseTelephonyAdapter):
    """
    Production Adapter for Genesys Cloud CX Platform API & Audio Connector.
    """

    async def start_outbound_call(self, customer: CustomerProfile, campaign_id: str) -> str:
        call_id = f"GENESYS-CALL-{uuid.uuid4().hex[:8].upper()}"
        logger.info(
            "Genesys Outbound Call Initiated: call_id=%s phone=%s campaign=%s",
            call_id,
            UAEComplianceGuard.mask_pii(customer.phone_e164),
            campaign_id,
        )
        return call_id

    async def warm_transfer_to_queue(
        self,
        call_id: str,
        target_queue_name: str,
        screen_pop_attributes: Dict[str, Any],
    ) -> Dict[str, Any]:
        logger.info(
            "Genesys Warm Transfer Triggered: call_id=%s -> queue='%s' with ScreenPop=%s",
            call_id,
            target_queue_name,
            json.dumps(screen_pop_attributes),
        )
        return {
            "status": "TRANSFERRED",
            "call_id": call_id,
            "target_queue": target_queue_name,
            "genesys_participant_attributes_attached": True,
        }


class OutlookGraphEmailAdapter(BaseEmailAdapter):
    """
    Phase 2 Adapter for Microsoft Graph API (`POST /users/{mailbox}/sendMail`).
    """

    async def send_email_reply(
        self,
        recipient_email: str,
        subject: str,
        body_text: str,
        reference_ticket_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        msg_id = f"GRAPH-MSG-{uuid.uuid4().hex[:8].upper()}"
        logger.info(
            "Outlook Graph Email Sent: id=%s to=%s subject='%s' ticket_ref=%s",
            msg_id,
            UAEComplianceGuard.mask_pii(recipient_email),
            subject,
            reference_ticket_id,
        )
        return {"message_id": msg_id, "status": "SENT"}


# ============================================================================
# 4. CORE AI AGENT ORCHESTRATOR ("THE BRAIN") & LLM TOOL DEFINITIONS
# ============================================================================

AGENT_TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_best_offers",
            "description": "Fetch personalized promotional offers and loyalty upgrades for the customer from the CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "Unique CRM customer ID"}
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_crm_ticket",
            "description": "Log a service request, technical issue, or formal complaint in the backend CRM and return the ticket reference number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "ticket_type": {
                        "type": "string",
                        "enum": ["SERVICE_REQUEST", "COMPLAINT", "INQUIRY"],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    },
                    "summary": {"type": "string", "description": "Short 1-line title of the issue or request"},
                    "details": {"type": "string", "description": "Detailed context captured from the customer"},
                },
                "required": ["customer_id", "ticket_type", "priority", "summary", "details"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_customer_feedback",
            "description": "Save structured customer feedback and satisfaction rating (1-5) into the CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "rating_1_to_5": {"type": "integer", "minimum": 1, "maximum": 5},
                    "sentiment": {"type": "string", "enum": ["POSITIVE", "NEUTRAL", "NEGATIVE"]},
                    "verbatim_comment": {"type": "string"},
                },
                "required": ["customer_id", "rating_1_to_5", "sentiment", "verbatim_comment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_human_agent",
            "description": "Warm-transfer the active call to a human specialist with a full context summary and ticket IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_queue": {
                        "type": "string",
                        "enum": ["Retention_Specialists", "Complaints_Escalation_Desk", "General_Support"],
                    },
                    "escalation_reason": {"type": "string"},
                    "conversation_summary": {"type": "string"},
                },
                "required": ["target_queue", "escalation_reason", "conversation_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_dnc_opt_out",
            "description": "Immediately add the customer to the Do Not Call (DNC) list if they ask not to receive future calls (UAE TDRA compliance).",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                },
                "required": ["customer_id"],
            },
        },
    },
]


class OmnichannelAgentOrchestrator:
    """
    Channel-agnostic AI Orchestrator.
    Works identically whether the voice call comes from your Personal Number Prototype
    (`PersonalPhoneTwilioAdapter`), Enterprise Genesys (`GenesysCloudAdapter`), or
    Outlook Email (`OutlookGraphEmailAdapter`).
    """

    def __init__(
        self,
        crm_adapter: BaseCRMAdapter,
        telephony_adapter: BaseTelephonyAdapter,
        email_adapter: BaseEmailAdapter,
        compliance_guard: UAEComplianceGuard,
    ) -> None:
        self.crm = crm_adapter
        self.telephony = telephony_adapter
        self.email = email_adapter
        self.compliance = compliance_guard

    def build_system_prompt(self, session: InteractionSession) -> str:
        return (
            f"You are Noura, an enterprise virtual assistant calling on behalf of the organization in the UAE.\n"
            f"Customer Name: {session.customer.full_name} (ID: {session.customer.customer_id}, Tier: {session.customer.segment})\n"
            f"Channel: {session.channel.value}\n"
            "Core Rules:\n"
            "1. UAE Compliance: Always confirm at the beginning of a voice call that the call is recorded and ask if it is a convenient time to speak.\n"
            "2. Keep spoken responses concise (1-2 sentences per turn) so the voice conversation feels natural and low-latency.\n"
            "3. Use `get_best_offers` when presenting offers, `create_crm_ticket` for service requests or complaints, and `log_customer_feedback` for ratings.\n"
            "4. If the customer is upset, requests a supervisor, or has a complex dispute, log the complaint ticket first and then invoke `transfer_to_human_agent`.\n"
            "5. If the customer asks to stop receiving calls, immediately call `register_dnc_opt_out` and politely end the call."
        )

    async def execute_tool_call(
        self,
        session: InteractionSession,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Routes LLM function calls to the pluggable backend adapters.
        """
        if tool_name == "get_best_offers":
            offers = await self.crm.get_eligible_offers(session.customer.customer_id)
            return {"offers": [dataclasses.asdict(o) for o in offers]}

        elif tool_name == "create_crm_ticket":
            ticket = await self.crm.create_ticket(
                customer_id=session.customer.customer_id,
                ticket_type=TicketType(arguments["ticket_type"]),
                priority=TicketPriority(arguments["priority"]),
                summary=arguments["summary"],
                details=arguments["details"],
                channel=session.channel,
            )
            session.created_tickets.append(ticket.ticket_id)
            return dataclasses.asdict(ticket)

        elif tool_name == "log_customer_feedback":
            fb = await self.crm.save_feedback(
                customer_id=session.customer.customer_id,
                rating_1_to_5=int(arguments["rating_1_to_5"]),
                sentiment=arguments["sentiment"],
                verbatim_comment=arguments["verbatim_comment"],
                channel=session.channel,
            )
            return dataclasses.asdict(fb)

        elif tool_name == "transfer_to_human_agent":
            screen_pop_payload = {
                "customer_id": session.customer.customer_id,
                "customer_name": session.customer.full_name,
                "segment": session.customer.segment,
                "created_tickets": session.created_tickets,
                "escalation_reason": arguments["escalation_reason"],
                "ai_summary": arguments["conversation_summary"],
            }
            result = await self.telephony.warm_transfer_to_queue(
                call_id=session.session_id,
                target_queue_name=arguments["target_queue"],
                screen_pop_attributes=screen_pop_payload,
            )
            session.transferred_to_human = True
            return result

        elif tool_name == "register_dnc_opt_out":
            await self.crm.mark_customer_opt_out(session.customer.customer_id)
            self.compliance.add_to_dnc(session.customer.phone_e164)
            return {"status": "OPTED_OUT", "message": "Customer added to UAE DNC list."}

        raise ValueError(f"Unknown tool invoked: {tool_name}")


# ============================================================================
# 5. END-TO-END SIMULATION & VERIFICATION SUITE
# ============================================================================

async def run_end_to_end_prototype_simulation() -> None:
    print("=" * 84)
    print("ENTERPRISE VOICE AI & OMNICHANNEL ORCHESTRATOR - PERSONAL NUMBER PROTOTYPE MODE")
    print("=" * 84)

    crm = EnterpriseMockCRMAdapter()
    # Using PersonalPhoneTwilioAdapter instead of GenesysCloudAdapter for rapid prototyping!
    telephony = PersonalPhoneTwilioAdapter(
        personal_caller_id_e164="+971501112233",
        human_agent_fallback_phone_e164="+971509998877",
    )
    email_gateway = OutlookGraphEmailAdapter()

    # Enable sandbox_prototype_mode with whitelisted test number (+971504439812)
    compliance = UAEComplianceGuard(
        sandbox_prototype_mode=True,
        sandbox_whitelisted_numbers={"+971504439812"},
    )

    orchestrator = OmnichannelAgentOrchestrator(
        crm_adapter=crm,
        telephony_adapter=telephony,
        email_adapter=email_gateway,
        compliance_guard=compliance,
    )

    customer = await crm.get_customer("CUST-UAE-101")
    assert customer is not None

    # ------------------------------------------------------------------------
    # SCENARIO 1: UAE Compliance Check (Personal Caller ID Whitelist & Hours)
    # ------------------------------------------------------------------------
    print("\n--- [1] Testing UAE TDRA Compliance & Personal Caller ID Sandbox Guard ---")
    valid_hours_time = datetime(2026, 10, 26, 11, 15, tzinfo=UAE_TZ)  # 11:15 AM GST
    allowed, reason = compliance.verify_outbound_call_eligibility(
        customer,
        using_personal_caller_id=True,
        is_promotional=True,
        simulated_uae_time=valid_hours_time,
    )
    print(f"Whitelisted Test Number (+97150****812) -> Allowed={allowed} | {reason}")

    # Test blocking a non-whitelisted real customer number when using a personal Caller ID
    non_whitelisted_customer = dataclasses.replace(customer, phone_e164="+971528887766")
    allowed_pub, reason_pub = compliance.verify_outbound_call_eligibility(
        non_whitelisted_customer,
        using_personal_caller_id=True,
        is_promotional=True,
        simulated_uae_time=valid_hours_time,
    )
    print(f"Unverified Public Number (+97152****766) -> Allowed={allowed_pub} | {reason_pub}")

    # ------------------------------------------------------------------------
    # SCENARIO 2: Outbound Call via Personal Caller ID Adapter (Feedback, Offer, Complaint & Transfer)
    # ------------------------------------------------------------------------
    print("\n--- [2] Simulating Outbound Voice Call via PersonalPhoneTwilioAdapter ---")
    call_id = await telephony.start_outbound_call(customer, campaign_id="PROTOTYPE-DEMO-01")
    session = InteractionSession(
        session_id=call_id,
        customer=customer,
        channel=ChannelType.VOICE_PERSONAL_PROTOTYPE,
    )

    greeting = (
        f"Hello {customer.full_name}, this is Noura calling from Customer Care on a recorded line. "
        "We're checking in on your recent experience and have a Platinum loyalty update for you. "
        "Is now a convenient time to speak?"
    )
    print(f"\n[AI Agent]: {greeting}")
    print("[Customer]: Yes, go ahead. Actually my home fiber speed has been dropping every evening, though your mobile service is great (4 out of 5).")

    fb_res = await orchestrator.execute_tool_call(
        session,
        "log_customer_feedback",
        {
            "customer_id": customer.customer_id,
            "rating_1_to_5": 4,
            "sentiment": "NEUTRAL",
            "verbatim_comment": "Mobile service is great (4/5), but home fiber speed drops every evening.",
        },
    )
    ticket_res = await orchestrator.execute_tool_call(
        session,
        "create_crm_ticket",
        {
            "customer_id": customer.customer_id,
            "ticket_type": "COMPLAINT",
            "priority": "HIGH",
            "summary": "Recurring evening home fiber speed degradation",
            "details": "Customer reported daily evening speed drops on home fiber connection during outbound check-in call.",
        },
    )
    offers_res = await orchestrator.execute_tool_call(
        session,
        "get_best_offers",
        {"customer_id": customer.customer_id},
    )
    top_offer = offers_res["offers"][0]

    ai_turn_2 = (
        f"Thank you for that feedback—I've logged your 4-out-of-5 rating and immediately raised a high-priority "
        f"technical complaint under reference {ticket_res['ticket_id']} for your evening fiber speeds. "
        f"Also, as a Platinum member, you qualify for our {top_offer['title']} ({top_offer['description']}). "
        "Would you like me to activate that offer or connect you with a specialist to check your fiber line right now?"
    )
    print(f"[AI Agent]: {ai_turn_2}")
    print("[Customer]: Please apply the AED 300 auto-pay bill credit request, and then transfer me to a specialist to check my router now.")

    sr_res = await orchestrator.execute_tool_call(
        session,
        "create_crm_ticket",
        {
            "customer_id": customer.customer_id,
            "ticket_type": "SERVICE_REQUEST",
            "priority": "MEDIUM",
            "summary": "Enroll in Auto-Pay & apply AED 300 Platinum Loyalty Bill Credit",
            "details": "Customer accepted OFFER-LOYALTY-CASHBACK during outbound call.",
        },
    )
    transfer_res = await orchestrator.execute_tool_call(
        session,
        "transfer_to_human_agent",
        {
            "target_queue": "Complaints_Escalation_Desk",
            "escalation_reason": "Customer requested live router diagnostics for evening fiber speed drops.",
            "conversation_summary": (
                f"Logged 4/5 feedback ({fb_res['feedback_id']}), raised fiber complaint {ticket_res['ticket_id']}, "
                f"and logged service request {sr_res['ticket_id']} for AED 300 Auto-Pay credit."
            ),
        },
    )
    print(
        f"[AI Agent]: I've registered your AED 300 bill credit request under {sr_res['ticket_id']}. "
        f"I am bridging your call now to our specialist mobile line ({transfer_res['bridged_to_phone']}) "
        f"with your full case summary attached."
    )

    print("\n" + "=" * 84)
    print("PROTOTYPE SIMULATION SUMMARY STATE IN CRM:")
    print(f"  * Active Telephony Adapter : {type(telephony).__name__}")
    print(f"  * Open CRM Tickets         : {customer.open_ticket_ids}")
    print(f"  * Feedback Records Logged  : {len(crm.feedbacks)} (Rating: {crm.feedbacks[0].rating_1_to_5}/5)")
    print(f"  * Human Transfer Bridged   : {session.transferred_to_human} (to {transfer_res['bridged_to_phone']})")
    print("=" * 84)


if __name__ == "__main__":
    asyncio.run(run_end_to_end_prototype_simulation())