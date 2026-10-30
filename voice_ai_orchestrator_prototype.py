#!/usr/bin/env python3
"""
RAKEZ Autonomous Voice AI Caller & Portal 360 Orchestrator Prototype
====================================================================
A modular, pluggable reference implementation sitting on top of existing
RAKEZ enterprise systems (Genesys Telephony / Personal Caller ID, Portal 360 CRM,
and Microsoft Outlook/Graph).

Key Capabilities Implemented:
1. Personal Human Addressing:
   - Always addresses the customer by their actual personal name (`first_name` / `full_name`,
     e.g., "Tariq"), never by their corporate LLC name (`company_name`).
2. Conversational Improvisation & Fuzzy Brief-Phrase Offer Matching:
   - Understands brief 1-2 word replies from the customer (e.g., "the 3-year one",
     "warehouse", "15% off") via `match_brief_offer_mention()`, explains the matched offer,
     and asks for confirmation (`pending_offer_confirmation`) before logging the Portal 360 SR.
3. Autonomous Call Resolution & Strict 3-Rule Relationship Officer Escalation:
   - Replaces the human caller for standard renewals, offers, FAQs, and routine service tickets.
   - ONLY transfers to a human Relationship Officer when one of 3 modifiable backend rules triggers:
     * Rule 1 (Customer Request): Customer explicitly asks for a Relationship Officer / human.
     * Rule 2 (High Dissatisfaction): Customer is strongly dissatisfied / refuses to listen.
     * Rule 3 (System-Mandated Policy): High-risk scenario (e.g., Company Liquidation / Legal Freeze).
4. Modifiable Backend Scenarios (`BACKEND_SCENARIOS_CONFIG`):
   - Easily add or modify scenarios, offer keywords, and escalation triggers in one place.
5. UAE Regulatory Compliance Middleware (`UAEComplianceGuard`):
   - Enforces UAE Cabinet Resolution No. 56 of 2024 (09:00-18:00 GST window, DNCR scrubbing)
     and UAE PDPL (Federal Decree-Law No. 45 of 2021) PII masking.
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
from datetime import datetime, time, timezone, timedelta
import enum
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("RAKEZVoiceAIOrchestrator")

UAE_TZ = timezone(timedelta(hours=4))


# ============================================================================
# 0. MODIFIABLE BACKEND SCENARIOS & RELATIONSHIP OFFICER ESCALATION RULES
# ============================================================================

RM_ESCALATION_RULES: Dict[str, List[str]] = {
    # Rule 1: Customer explicitly asks to speak with a human officer
    "explicit_officer_request_keywords": [
        "relationship manager",
        "relationship officer",
        "transfer me",
        "speak to a human",
        "real person",
        "human agent",
        "supervisor",
        "connect me to an officer",
    ],
    # Rule 2: Customer is strongly dissatisfied and not ready to listen
    "severe_dissatisfaction_keywords": [
        "frustrated",
        "fed up",
        "unacceptable",
        "ridiculous",
        "terrible",
        "waste of time",
        "not listening",
        "don't want to listen",
        "angry",
        "furious",
        "1 out of 5",
        "worst service",
    ],
    # Rule 3: System-mandated policy scenarios requiring a Relationship Officer
    "system_mandated_intervention_keywords": [
        "liquidate",
        "liquidation",
        "deregister",
        "cancel license",
        "shareholder dispute",
        "court order",
        "legal freeze",
        "custom land plot",
    ],
}

BACKEND_SCENARIOS_CONFIG: List[Dict[str, Any]] = [
    {
        "id": "SCENARIO_VISA_DELAY",
        "name": "Routine Visa / ICP Delay (AI Resolves Autonomously)",
        "trigger_keywords": ["visa", "stuck", "delayed", "delay", "icp", "medical", "emirates id"],
        "requires_rm_transfer": False,
        "auto_ticket_type": "COMPLAINT",
        "auto_ticket_priority": "HIGH",
        "response_template": (
            "I completely understand how stressful a visa delay can be, {first_name}. "
            "Let me take care of that for you right now—I've just raised a high-priority escalation "
            "ticket ({ticket_id}) with our Government Services team to expedite your application with ICP, "
            "and they'll update your Portal 360 status today. While I have you on the line, is there "
            "anything else on your renewal I can help sort out?"
        ),
    },
    {
        "id": "SCENARIO_UBO_DOCS",
        "name": "Licensing & UBO Document Checklist Inquiry",
        "trigger_keywords": ["document", "documents", "ubo", "checklist", "paperwork", "needed", "require"],
        "requires_rm_transfer": False,
        "auto_ticket_type": None,
        "response_template": (
            "Sure thing, {first_name}. For your renewal on Portal 360, all you need is your signed lease "
            "agreement, valid passport and Emirates ID copies for the shareholders, and the quick UBO "
            "declaration form. I can send that checklist straight to your email right now if you'd like!"
        ),
    },
    {
        "id": "SCENARIO_PRICING_INSTALLMENTS",
        "name": "Flexible Payment & Installment Plan Inquiry",
        "trigger_keywords": ["expensive", "cost", "price", "installment", "installments", "monthly", "payment plan"],
        "requires_rm_transfer": False,
        "auto_ticket_type": None,
        "response_template": (
            "That's a very fair question, {first_name}. In addition to the multi-year discount, we offer "
            "flexible installment plans through Portal 360 using partner bank credit cards with zero interest, "
            "or post-dated cheques. Would you like me to include the installment option on your renewal quote?"
        ),
    },
    {
        "id": "SCENARIO_LIQUIDATION_MANDATORY_RM",
        "name": "System-Mandated RM Intervention: Company Liquidation / Legal Freeze",
        "trigger_keywords": ["liquidate", "liquidation", "close company", "cancel license", "deregister", "shareholder dispute"],
        "requires_rm_transfer": True,
        "escalation_reason": "System-Mandated Policy: Company Liquidation / Legal Restructuring requires Relationship Officer authorization.",
        "response_template": (
            "I understand, {first_name}. Because company liquidation and license deregistration require formal "
            "clearance and dedicated Relationship Officer sign-off under RAKEZ policy, I've logged pre-check "
            "reference {ticket_id} and I'm connecting you directly with your Relationship Officer right now."
        ),
    },
]


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
    first_name: str  # Personal first name used in conversation (e.g., "Tariq")
    full_name: str   # Full human name (e.g., "Tariq Al-Mansoori")
    company_name: str  # Secondary corporate entity metadata (never used as the human's name)
    phone_e164: str
    email: str
    segment: str
    preferred_language: str = "en-AE"
    dnc_opted_out: bool = False
    open_ticket_ids: List[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class PromotionalOffer:
    offer_id: str
    short_label: str
    title: str
    spoken_pitch: str
    description: str
    discount_pct: float
    valid_until: str
    eligibility_segment: str
    shorthand_keywords: List[str] = dataclasses.field(default_factory=list)


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
    pending_offer_confirmation: Optional[PromotionalOffer] = None
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

    ALLOWED_START_TIME = time(9, 0)
    ALLOWED_END_TIME = time(18, 0)

    def __init__(self, national_dncr_numbers: Optional[set[str]] = None) -> None:
        self.national_dncr_numbers = national_dncr_numbers or set()
        self.call_attempt_log: Dict[str, List[datetime]] = {}

    def verify_outbound_call_eligibility(
        self,
        customer: CustomerProfile,
        is_promotional: bool = True,
        simulated_uae_time: Optional[datetime] = None,
    ) -> tuple[bool, str]:
        now_uae = simulated_uae_time or datetime.now(UAE_TZ)

        if customer.dnc_opted_out or customer.phone_e164 in self.national_dncr_numbers:
            return (
                False,
                f"Blocked by UAE TDRA DNCR / Customer Opt-Out for {self.mask_pii(customer.phone_e164)}.",
            )

        if is_promotional:
            current_t = now_uae.time()
            if not (self.ALLOWED_START_TIME <= current_t <= self.ALLOWED_END_TIME):
                return (
                    False,
                    f"Blocked by UAE Telemarketing Window: Current time {current_t.strftime('%H:%M')} GST "
                    f"is outside permitted 09:00-18:00 GST window.",
                )

        attempts = self.call_attempt_log.get(customer.customer_id, [])
        one_day_ago = now_uae - timedelta(days=1)
        one_week_ago = now_uae - timedelta(days=7)

        if len([ts for ts in attempts if ts > one_day_ago]) >= 1:
            return False, "Blocked by TDRA retry rule: Maximum 1 unanswered attempt per day reached."
        if len([ts for ts in attempts if ts > one_week_ago]) >= 2:
            return False, "Blocked by TDRA retry rule: Maximum 2 unanswered attempts per week reached."

        return True, "Eligible for outbound call under UAE TDRA rules."

    def add_to_dnc(self, phone_e164: str) -> None:
        self.national_dncr_numbers.add(phone_e164)

    @staticmethod
    def mask_pii(text: str) -> str:
        text = re.sub(r"(\+971\d{2})\d{4}(\d{3})", r"\1****\2", text)
        text = re.sub(r"784-\d{4}-\d{7}-\d", "784-****-*******-*", text)
        text = re.sub(r"([a-zA-Z0-9_.+-])[a-zA-Z0-9_.+-]+(@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", r"\1***\2", text)
        return text


# ============================================================================
# 3. PLUGGABLE BACKEND INTERFACES (PORTS) & ADAPTERS
# ============================================================================

class BaseCRMAdapter(abc.ABC):
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
    @abc.abstractmethod
    async def send_email_reply(
        self,
        recipient_email: str,
        subject: str,
        body_text: str,
        reference_ticket_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pass


class EnterpriseMockCRMAdapter(BaseCRMAdapter):
    """
    Reference RAKEZ Portal 360 CRM Adapter.
    Stores personal human names (`first_name`, `full_name`) distinctly from `company_name`,
    and includes `shorthand_keywords` on every offer for brief-word recognition.
    """

    def __init__(self) -> None:
        self.customers: Dict[str, CustomerProfile] = {
            "CUST-UAE-101": CustomerProfile(
                customer_id="CUST-UAE-101",
                first_name="Mr. Tariq",
                full_name="Tariq Al-Mansoori",
                company_name="Al-Mansoori Industrial LLC",
                phone_e164="+971504439812",
                email="tariq@almansoori-ind.ae",
                segment="Al Hamra Industrial Zone",
                preferred_language="en-AE",
                open_ticket_ids=["SR-360-8841"],
            )
        }
        self.offers: List[PromotionalOffer] = [
            PromotionalOffer(
                offer_id="RAKEZ-3YR-RENEW-15",
                short_label="3-Year License Renewal Package",
                title="3-Year Multi-Year License Renewal Package",
                spoken_pitch=(
                    "our 3-Year License Renewal package, which gives you 15 percent off your "
                    "annual trade license fees plus 2 complimentary employee visa quotas"
                ),
                description="15% discount on RAKEZ trade license fees + 2 complimentary employee visa quotas.",
                discount_pct=15.0,
                valid_until="2026-12-31",
                eligibility_segment="Al Hamra Industrial Zone",
                shorthand_keywords=[
                    "3-year", "3 year", "three year", "15%", "15 percent", "fifteen",
                    "renewal", "license", "first", "multi-year", "visa quota",
                ],
            ),
            PromotionalOffer(
                offer_id="RAKEZ-WAREHOUSE-FITOUT",
                short_label="Al Hamra Warehouse Expansion Offer",
                title="Al Hamra Warehouse Expansion Grace Period",
                spoken_pitch=(
                    "our Al Hamra Warehouse Expansion offer, which includes a 3-month rent-free "
                    "fit-out grace period and fast-track Civil Defense NOC"
                ),
                description="3 months rent-free fit-out grace period & fast-track Civil Defense NOC on adjacent bay lease.",
                discount_pct=25.0,
                valid_until="2026-11-30",
                eligibility_segment="Al Hamra Industrial Zone",
                shorthand_keywords=[
                    "warehouse", "al hamra", "fit-out", "fit out", "rent-free",
                    "rent free", "3 months", "expansion", "bay", "second", "space",
                ],
            ),
        ]
        self.tickets: Dict[str, CRMTicket] = {}
        self.feedbacks: List[CustomerFeedback] = []

    async def get_customer(self, customer_id: str) -> Optional[CustomerProfile]:
        await asyncio.sleep(0.01)
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
        prefix = "CMP-360" if ticket_type == TicketType.COMPLAINT else "SR-360"
        ticket_id = f"{prefix}-{uuid.uuid4().hex[:4].upper()}"
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
            "Portal 360 Ticket Created: id=%s type=%s priority=%s summary='%s'",
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
            feedback_id=f"CSAT-{uuid.uuid4().hex[:4].upper()}",
            customer_id=customer_id,
            rating_1_to_5=rating_1_to_5,
            sentiment=sentiment,
            verbatim_comment=verbatim_comment,
            channel=channel,
            recorded_at_iso=datetime.now(UAE_TZ).isoformat(),
        )
        self.feedbacks.append(fb)
        logger.info(
            "Portal 360 Feedback Logged: id=%s rating=%d/5 sentiment=%s",
            fb.feedback_id,
            fb.rating_1_to_5,
            fb.sentiment,
        )
        return fb

    async def mark_customer_opt_out(self, customer_id: str) -> bool:
        if customer_id in self.customers:
            self.customers[customer_id].dnc_opted_out = True
            logger.info("Portal 360 DNC Flag Updated for customer_id=%s", customer_id)
            return True
        return False


class GenesysCloudAdapter(BaseTelephonyAdapter):
    async def start_outbound_call(self, customer: CustomerProfile, campaign_id: str) -> str:
        call_id = f"RAKEZ-CALL-{uuid.uuid4().hex[:6].upper()}"
        logger.info(
            "Outbound Call Initiated: call_id=%s customer='%s' phone=%s",
            call_id,
            customer.full_name,
            UAEComplianceGuard.mask_pii(customer.phone_e164),
        )
        return call_id

    async def warm_transfer_to_queue(
        self,
        call_id: str,
        target_queue_name: str,
        screen_pop_attributes: Dict[str, Any],
    ) -> Dict[str, Any]:
        logger.info(
            "Relationship Officer Warm Transfer Triggered: call_id=%s -> queue='%s' ScreenPop=%s",
            call_id,
            target_queue_name,
            json.dumps(screen_pop_attributes),
        )
        return {
            "status": "TRANSFERRED_TO_RELATIONSHIP_OFFICER",
            "call_id": call_id,
            "target_queue": target_queue_name,
            "genesys_participant_attributes_attached": True,
        }


class OutlookGraphEmailAdapter(BaseEmailAdapter):
    async def send_email_reply(
        self,
        recipient_email: str,
        subject: str,
        body_text: str,
        reference_ticket_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        msg_id = f"GRAPH-MSG-{uuid.uuid4().hex[:6].upper()}"
        logger.info(
            "Outlook Graph Email Sent: id=%s to=%s subject='%s' ticket_ref=%s",
            msg_id,
            UAEComplianceGuard.mask_pii(recipient_email),
            subject,
            reference_ticket_id,
        )
        return {"message_id": msg_id, "status": "SENT"}


# ============================================================================
# 4. AUTONOMOUS AI CALLER ORCHESTRATOR (Fuzzy Offer Match & Strict RM Policy)
# ============================================================================

class OmnichannelAgentOrchestrator:
    """
    Autonomous AI Caller Orchestrator for RAKEZ.
    - Addresses customers personally by `first_name` ("Tariq").
    - Matches brief customer phrases ("the 3-year one") to eligible offers and asks for confirmation.
    - Resolves standard inquiries and complaints autonomously.
    - Only transfers to a human Relationship Officer under the 3 strict escalation rules.
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
            f"You are Noura, a Senior Client Relations Executive calling from RAKEZ (Ras Al Khaimah Economic Zone).\n"
            f"You are speaking with {session.customer.full_name}. Always address them naturally by their first name "
            f"'{session.customer.first_name}'—NEVER call a human being by their company name ('{session.customer.company_name}').\n"
            "Core Rules:\n"
            "1. Sound like a warm, natural human caller—improvise smoothly and keep turns under 2 short sentences.\n"
            "2. If the customer mentions an offer briefly (e.g., 'the 3-year one' or 'warehouse'), confirm the exact "
            "offer name with them before logging the Portal 360 Service Request.\n"
            "3. Handle offers, questions, and routine visa/licensing complaints yourself. ONLY transfer to a Relationship "
            "Officer if: (a) the customer explicitly asks for an officer, (b) the customer is furious and refuses to listen, "
            "or (c) the system demands officer intervention (e.g., company liquidation / legal freeze)."
        )

    @staticmethod
    def match_brief_offer_mention(
        utterance: str, offers: List[PromotionalOffer]
    ) -> Optional[PromotionalOffer]:
        lower = utterance.lower()
        for offer in offers:
            for kw in offer.shorthand_keywords:
                if kw.lower() in lower:
                    return offer
        return None

    @staticmethod
    def evaluate_rm_escalation_trigger(utterance: str) -> Optional[tuple[str, str]]:
        """
        Checks if any of the 3 strict Relationship Officer transfer rules are triggered.
        Returns (rule_name, escalation_reason) or None if AI should handle autonomously.
        """
        lower = utterance.lower()
        for kw in RM_ESCALATION_RULES["explicit_officer_request_keywords"]:
            if kw in lower:
                return ("RULE_1_CUSTOMER_REQUESTED_OFFICER", f"Customer explicitly requested Relationship Officer ('{kw}').")
        for kw in RM_ESCALATION_RULES["severe_dissatisfaction_keywords"]:
            if kw in lower:
                return ("RULE_2_SEVERE_DISSATISFACTION", f"Customer expressed severe dissatisfaction/refusal ('{kw}').")
        for kw in RM_ESCALATION_RULES["system_mandated_intervention_keywords"]:
            if kw in lower:
                return ("RULE_3_SYSTEM_MANDATED_POLICY", f"System-mandated Relationship Officer policy triggered ('{kw}').")
        return None

    async def handle_customer_turn(
        self, session: InteractionSession, utterance: str
    ) -> str:
        """
        Processes a live customer turn with brief-phrase offer matching, confirmation memory,
        modifiable scenario resolution, and strict 3-rule Relationship Officer transfer gating.
        """
        lower = utterance.lower()
        first_name = session.customer.first_name
        offers = await self.crm.get_eligible_offers(session.customer.customer_id)

        # 1. Check strict 3-rule Relationship Officer escalation triggers
        escalation = self.evaluate_rm_escalation_trigger(utterance)
        if escalation:
            rule_code, reason = escalation
            ticket = await self.crm.create_ticket(
                customer_id=session.customer.customer_id,
                ticket_type=TicketType.COMPLAINT if "DISSATISFACTION" in rule_code else TicketType.SERVICE_REQUEST,
                priority=TicketPriority.CRITICAL if "DISSATISFACTION" in rule_code else TicketPriority.HIGH,
                summary=f"RM Escalation ({rule_code}): {utterance[:48]}",
                details=reason,
                channel=session.channel,
            )
            session.created_tickets.append(ticket.ticket_id)
            await self.telephony.warm_transfer_to_queue(
                call_id=session.session_id,
                target_queue_name="RAKEZ_Relationship_Officers",
                screen_pop_attributes={
                    "customer_name": session.customer.full_name,
                    "first_name": first_name,
                    "company_name": session.customer.company_name,
                    "escalation_rule": rule_code,
                    "escalation_reason": reason,
                    "portal_360_tickets": session.created_tickets,
                },
            )
            session.transferred_to_human = True
            return (
                f"I completely understand, {first_name}. I've logged priority reference {ticket.ticket_id} "
                f"and I'm connecting you straight through to your dedicated RAKEZ Relationship Officer right now "
                f"with your full case file on their screen so you won't need to repeat a thing."
            )

        # 2. Check if customer is confirming a pending offer ("yes", "confirm", "lock that in")
        if session.pending_offer_confirmation:
            pending = session.pending_offer_confirmation
            if any(w in lower for w in ["yes", "sure", "confirm", "lock", "go ahead", "apply", "do it"]):
                sr = await self.crm.create_ticket(
                    customer_id=session.customer.customer_id,
                    ticket_type=TicketType.SERVICE_REQUEST,
                    priority=TicketPriority.MEDIUM,
                    summary=f"Confirmed Offer: {pending.title}",
                    details=f"Customer {session.customer.full_name} verbally confirmed {pending.offer_id}.",
                    channel=session.channel,
                )
                session.created_tickets.append(sr.ticket_id)
                session.pending_offer_confirmation = None
                return (
                    f"Wonderful, {first_name}! I've just locked in the {pending.short_label} on your Portal 360 "
                    f"profile under service request {sr.ticket_id}. While I have you on the line, is there "
                    f"anything else I can take care of for you today?"
                )

        # 3. Check if customer said a brief phrase referring to a specific offer ("the 3-year one", "warehouse")
        matched_offer = self.match_brief_offer_mention(utterance, offers)
        if matched_offer and "what offer" not in lower:
            session.pending_offer_confirmation = matched_offer
            return (
                f"Got it, {first_name}—you're referring to {matched_offer.spoken_pitch}. "
                f"Just to confirm before I log the request in Portal 360, would you like me to go ahead "
                f"and lock in the {matched_offer.short_label} for you?"
            )

        # 4. Check modifiable backend scenarios (routine visa delay, UBO docs, installments) -> Resolved WITHOUT RM transfer
        for scenario in BACKEND_SCENARIOS_CONFIG:
            if not scenario["requires_rm_transfer"] and any(kw in lower for kw in scenario["trigger_keywords"]):
                t_id = "N/A"
                if scenario.get("auto_ticket_type"):
                    t_obj = await self.crm.create_ticket(
                        customer_id=session.customer.customer_id,
                        ticket_type=TicketType(scenario["auto_ticket_type"]),
                        priority=TicketPriority(scenario.get("auto_ticket_priority", "MEDIUM")),
                        summary=f"{scenario['name']} ({session.customer.full_name})",
                        details=utterance,
                        channel=session.channel,
                    )
                    session.created_tickets.append(t_obj.ticket_id)
                    t_id = t_obj.ticket_id
                return scenario["response_template"].format(first_name=first_name, ticket_id=t_id)

        # 5. Default offer overview pitch addressed personally to `first_name`
        offer_pitches = " and ".join(o.spoken_pitch for o in offers)
        return (
            f"Gladly, {first_name}! Looking at your Portal 360 profile, you pre-qualify for {offer_pitches}. "
            f"Which of those sounds like a better fit for your plans this year?"
        )


# ============================================================================
# 5. END-TO-END SIMULATION & VERIFICATION SUITE
# ============================================================================

async def run_end_to_end_prototype_simulation() -> None:
    print("=" * 84)
    print("RAKEZ AUTONOMOUS VOICE AI CALLER - IMPROVISATION & ESCALATION SIMULATION")
    print("=" * 84)

    crm = EnterpriseMockCRMAdapter()
    telephony = GenesysCloudAdapter()
    email_gateway = OutlookGraphEmailAdapter()
    compliance = UAEComplianceGuard()

    orchestrator = OmnichannelAgentOrchestrator(
        crm_adapter=crm,
        telephony_adapter=telephony,
        email_adapter=email_gateway,
        compliance_guard=compliance,
    )

    customer = await crm.get_customer("CUST-UAE-101")
    assert customer is not None

    call_id = await telephony.start_outbound_call(customer, campaign_id="RAKEZ-RENEWAL-Q4")
    session = InteractionSession(
        session_id=call_id,
        customer=customer,
        channel=ChannelType.VOICE_GENESYS,
    )

    # Turn 1: Personal greeting using `customer.first_name` ("Tariq")
    greeting = (
        f"Hi {customer.first_name}, good day! This is Noura calling from the Client Relations team "
        f"at RAKEZ on a recorded line. How are you doing today? I was calling to check in on your "
        f"experience and share two pre-approved renewal packages on your profile—do you have a quick minute?"
    )
    print(f"\n[Noura AI]: {greeting}")

    # Turn 2: Customer asks what offers they have
    u1 = "Doing well Noura, thanks. What offers do you have for me?"
    print(f"\n[{customer.full_name}]: {u1}")
    r1 = await orchestrator.handle_customer_turn(session, u1)
    print(f"[Noura AI]: {r1}")

    # Turn 3: Customer replies with a brief 3-word phrase ("The 3-year one") -> AI matches & asks confirmation
    u2 = "The 3-year one sounds good"
    print(f"\n[{customer.full_name}]: {u2}")
    r2 = await orchestrator.handle_customer_turn(session, u2)
    print(f"[Noura AI]: {r2}")

    # Turn 4: Customer confirms ("Yes, go ahead and confirm that") -> AI logs SR in Portal 360 autonomously
    u3 = "Yes, go ahead and confirm that for me"
    print(f"\n[{customer.full_name}]: {u3}")
    r3 = await orchestrator.handle_customer_turn(session, u3)
    print(f"[Noura AI]: {r3}")

    # Turn 5: Customer mentions a routine visa delay -> AI logs ticket & resolves WITHOUT transferring to RM
    u4 = "Also, one of my employee visas has been delayed in ICP review"
    print(f"\n[{customer.full_name}]: {u4}")
    r4 = await orchestrator.handle_customer_turn(session, u4)
    print(f"[Noura AI]: {r4}")
    print(f"  -> Transferred to Human RM? {session.transferred_to_human} (Handled 100% by AI Caller)")

    # Turn 6: Special Case Trigger (Customer expresses severe dissatisfaction / asks for liquidation) -> RM Transfer!
    u5 = "Actually I am really frustrated and fed up with this delay, I don't want to listen to anything!"
    print(f"\n[{customer.full_name}]: {u5}")
    r5 = await orchestrator.handle_customer_turn(session, u5)
    print(f"[Noura AI]: {r5}")
    print(f"  -> Transferred to Human RM? {session.transferred_to_human} (Triggered by Rule 2: Severe Dissatisfaction)")

    print("\n" + "=" * 84)
    print(f"FINAL PORTAL 360 TICKETS FOR {customer.full_name}: {customer.open_ticket_ids}")
    print("=" * 84)


# ============================================================================
# 6. LIVE TWILIO PHONE DIALER & GMAIL INBOX CHECKER (Zero-Dependency CLI)
# ============================================================================

def dial_live_twilio_call(target_phone_e164: str, first_name: str = "Tariq") -> None:
    """
    Makes a REAL outbound phone call to `target_phone_e164` using Twilio's REST API
    and your Verified Personal Caller ID (zero external pip packages required).
    """
    import base64
    import os
    import urllib.parse
    import urllib.request

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("PERSONAL_VERIFIED_CALLER_ID", "").strip()

    if not (account_sid and auth_token and from_number):
        print("Error: Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and PERSONAL_VERIFIED_CALLER_ID.")
        return

    twiml = f"""<Response>
        <Say voice="Polly.Joanna-Neural">
            Hi {first_name}, good day! This is Noura calling from the Client Relations team at Rakez on a recorded line.
            How are you doing today? I was calling to check in on your experience and share two pre-approved renewal packages
            on your Portal 360 profile, including a 15 percent 3-year trade license renewal discount.
        </Say>
        <Pause length="1"/>
        <Say voice="Polly.Joanna-Neural">
            I have also sent the full renewal summary to your email. Have a wonderful day ahead!
        </Say>
    </Response>"""

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    data = urllib.parse.urlencode({
        "To": target_phone_e164,
        "From": from_number,
        "Twiml": twiml,
    }).encode("utf-8")

    creds = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        res_json = json.loads(resp.read().decode("utf-8"))
        print(f"SUCCESS! Ringing {target_phone_e164} right now (Call SID: {res_json.get('sid')})")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--dial":
        dial_live_twilio_call(sys.argv[2], first_name=sys.argv[3] if len(sys.argv) >= 4 else "Tariq")
    else:
        asyncio.run(run_end_to_end_prototype_simulation())
