# Enterprise Voice AI & Omnichannel Agent System: Architecture, UAE Compliance & Build Plan

## 1. Executive Architecture: Pluggable Overlay Pattern

To sit cleanly on top of your existing enterprise stack without requiring core migrations, the system uses a **Hexagonal (Ports & Adapters) Overlay Architecture**. The AI Orchestrator ("The Brain") is completely decoupled from both the **communication channels** (Genesys Voice today, Microsoft Outlook Email tomorrow) and the **backend systems of record** (Salesforce, Dynamics 365, ServiceNow, or custom CRMs).

### Four-Layer Decoupled Stack

| Layer | Responsibility | Phase 1 (Voice & CRM MVP) | Phase 2 (Omnichannel Evolution) |
| :--- | :--- | :--- | :--- |
| **1. Channel Adapters (Ingress/Egress)** | Normalize real-time audio streams or text events into a unified session state. | [Genesys Cloud Audio Connector](https://help.mypurecloud.com/articles/about-audio-connector/) & [AudioHook Protocol](https://help.mypurecloud.com/articles/audiohook-monitor-overview/) (bidirectional WebSocket u-law/PCM 8kHz/16kHz audio) or SIP trunking. | [Microsoft Graph Mail REST API](https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview?view=graph-rest-1.0) webhook subscriptions for inbound emails & automated replies. |
| **2. Real-Time Voice & Media Pipeline** | Manage sub-800ms turn-taking, Voice Activity Detection (VAD), interruption handling (barge-in), and audio frames. | Open-source Python pipeline via [Pipecat](https://github.com/pipecat-ai/pipecat) or [LiveKit Agents](https://docs.livekit.io/agents/) + [Azure AI Speech (UAE North)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions) for STT/TTS. | Bypassed for text/email channels; routes parsed email threads directly to the Agent Orchestrator. |
| **3. Core Agent Orchestrator ("The Brain")** | Maintain conversation state, enforce UAE compliance guardrails, detect customer intent, and execute tool calls. | Stateful LLM engine (Azure OpenAI GPT-4o in `uaenorth` or self-hosted vLLM on UAE GPUs) with strict JSON schema function calling. | Shared multi-modal prompt & memory store linking prior voice transcripts with ongoing email threads. |
| **4. Backend Integration Layer (Connectors)** | Abstract CRM, ticketing, and telephony transfer APIs behind standardized Python interfaces (`BaseCRMAdapter`). | Pluggable REST adapters for customer lookup, personalized offer retrieval, ticket/complaint creation, feedback logging, and Genesys warm transfer. | Adds ERP/billing connectors, automated document parsing, and cross-channel SLA escalation triggers. |

---

## 2. In-House Voice AI Pipeline & Low-Latency Engineering

Building an in-house voice agent that feels natural requires keeping end-to-end voice-to-voice latency under **700–900ms**. Rather than chaining blocking HTTP requests, the voice pipeline must use **full-duplex asynchronous streaming** over WebSockets.

### Streaming Data Flow

1. **Outbound Call Initiation & Pre-Call Guardrails**:
   - The Campaign Scheduler pulls target customer records from the CRM Adapter.
   - Before dialing, the Compliance Guardrail verifies the UAE time window (09:00–18:00 local time) and checks the customer's number against the TDRA **Do Not Call Register (DNCR)** and internal opt-out list.
   - If cleared, the Telephony Adapter triggers an outbound call via the Genesys Cloud Outbound API and attaches the `customer_id` and `campaign_id` in SIP/UUI headers.
2. **Bidirectional Media Streaming**:
   - Once the customer answers, [Genesys Audio Connector](https://help.mypurecloud.com/articles/configure-and-activate-audio-connector-in-genesys-cloud/) establishes a WebSocket connection to your FastAPI Media Gateway and streams raw audio frames.
   - A local **Silero VAD (Voice Activity Detector)** monitors incoming audio in 30ms chunks. If the customer speaks while the AI is talking (**barge-in**), the pipeline immediately halts TTS playback and clears the outbound audio buffer.
3. **Streaming STT -> LLM Tool Execution -> Streaming TTS**:
   - Audio frames stream continuously into [Azure AI Speech STT](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions) (`en-AE` / `en-US` models hosted in `uaenorth`).
   - Finalized utterances are passed to the **Agent Orchestrator**, which evaluates conversation history and invokes tools (`get_best_offers`, `create_crm_request`, `log_customer_feedback`, or `transfer_to_human_agent`) asynchronously.
   - As the LLM streams tokens back, a sentence boundary detector chunks the text and streams it into TTS, sending audio frames back over the Genesys WebSocket before the LLM even finishes generating the full paragraph.

---

## 3. UAE Regulatory & Data Residency Compliance Blueprint

Operating an automated outbound calling and customer data platform in the United Arab Emirates requires strict adherence to federal data protection and telemarketing regulations. Build these controls directly into the orchestrator's middleware rather than treating them as an afterthought.

### Regulatory Requirements & Technical Controls

| Regulation / Authority | Core Mandate | Technical Implementation in Your Architecture |
| :--- | :--- | :--- |
| **[UAE Federal Decree-Law No. 45 of 2021 (PDPL)](https://u.ae/en/about-the-uae/digital-uae/data/data-protection-laws)** | Protects personal data processing, requires explicit consent, mandates data minimization, and restricts unauthorized cross-border data transfers. | Host all compute, databases, STT/TTS ([Azure AI Speech](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions)), and LLM inference within **UAE cloud regions** (`uaenorth` in Dubai or `me-central-1` on AWS UAE). Run a PII-redaction regex/NER filter on transcripts before writing application logs. |
| **[UAE Cabinet Resolution No. 56 of 2024 (Telemarketing Regulations)](https://uaelegislation.gov.ae/en/legislations/2519/)** | Strictly regulates marketing/outbound calls supervised by the **TDRA** (and Central Bank/SCA where applicable). | Enforce hard pre-call gates in the Dialer Service: <br>1. **DNCR Scrubbing**: Query the TDRA Do Not Connect Register and internal DNC table before every call.<br>2. **Calling Window Lock**: Reject outbound promotional calls outside **09:00 AM to 06:00 PM UAE time** (`Asia/Dubai`, UTC+4).<br>3. **Retry Caps**: Block re-dialing if a customer does not answer (max 1 attempt/day, max 2/week) or declines the offer on the first contact. |
| **Mandatory Call Recording & AI Disclosure** | Customers must be informed at the start of the call if the conversation is recorded and involves an automated system, and calls must originate from locally registered corporate numbers. | Configure the opening system prompt and deterministic greeting audio to state: *"Hello, I am calling from [Organization Name] on a recorded line. Is now a convenient time to speak?"* Log the customer's verbal consent timestamp in the CRM record. |
| **Immediate Opt-Out ("Right to Stop Processing")** | Under both [PDPL](https://uaelegislation.gov.ae/en/legislations/1972) and [Cabinet Resolution No. 56 of 2024](https://uaelegislation.gov.ae/en/legislations/2519/download), if a customer asks not to be called again, the organization must immediately cease marketing calls. | Expose a dedicated `register_dnc_opt_out` tool to the LLM. If the customer says *"Take me off your list"* or *"Stop calling"*, the AI immediately triggers the tool, updates the CRM DNC flag, apologizes politely, and terminates the call. |

---

## 4. Pluggable Adapter Design (CRM, Genesys & Future Outlook Email)

To ensure your system can connect to any underlying CRM or communication platform without rewriting agent logic, every external integration implements an abstract Python interface.

### Core Tool Contracts Exposed to the AI Agent

1. **`get_customer_context_and_offers(customer_id, category)`**:
   - Queries the CRM or Offer Engine for customer tier, open tickets, and ranked promotional offers matching the customer's profile.
2. **`create_service_or_complaint_ticket(customer_id, request_type, priority, summary, details)`**:
   - Raises a structured `SERVICE_REQUEST` or `COMPLAINT` in the backend CRM, returning a live `ticket_id` (e.g., `SR-2026-8841`) so the voice agent can read the reference number back to the customer during the call.
3. **`record_customer_feedback(customer_id, rating_score, sentiment, verbatim_notes)`**:
   - Stores CSAT/NPS scores (1–5 or 1–10) and structured feedback tags directly against the customer's interaction history.
4. **`escalate_to_human_agent(call_id, target_queue, escalation_reason, conversation_summary)`**:
   - Invokes the Genesys Cloud Conversations API to execute a **warm/consult transfer** or **blind transfer** to a specialized skill queue (e.g., `VIP_Retention` or `Complaints_Desk`), passing the AI-generated `conversation_summary` and `ticket_id` into Genesys Participant Attributes (Screen Pop) so the human agent never asks the customer to repeat themselves.

### Seamless Evolution to Phase 2 (Microsoft Outlook / Graph API)

Because the `AgentOrchestrator` accepts a normalized `InteractionTurn(channel="voice" | "email", customer_id=..., message=...)`, adding email support in Phase 2 requires **zero changes** to your CRM tools or business logic:
- **Inbound Email Ingestion**: Register a webhook subscription via the [Microsoft Graph API (`POST /subscriptions`)](https://learn.microsoft.com/en-us/graph/api/resources/mail-api-overview?view=graph-rest-1.0) watching your shared support mailbox (`support@your-org.ae`).
- **Context Continuity**: When an email arrives, the `OutlookChannelAdapter` resolves the sender's email to a `customer_id`, loads recent voice call summaries and open CRM tickets from the shared session store, and passes the email body to the `AgentOrchestrator`.
- **Automated Action & Reply**: The orchestrator invokes the exact same `create_service_or_complaint_ticket` tool if the email contains a new request, and drafts or sends a reply via [`POST /users/{id}/messages/{id}/reply`](https://learn.microsoft.com/en-us/graph/outlook-things-to-know-about-send-mail) (or queues it for human approval if confidence is low).

---

## 5. Step-by-Step Implementation Roadmap

### Phase 0: Local Prototype & CLI/WebSocket Simulator (Weeks 1–2)
- **Objective**: Validate the Agent Orchestrator, UAE compliance checks, LLM tool-calling accuracy, and mock CRM/Genesys adapters locally before touching live telecom trunks.
- **Deliverables**:
  - FastAPI orchestration service with `BaseCRMAdapter`, `BaseTelephonyAdapter`, and `UAEComplianceGuard`.
  - Interactive multi-turn simulator script (provided in `voice_ai_orchestrator_prototype.py`) testing offer recommendation, feedback capture, complaint ticket creation, and human agent warm transfer.

### Phase 1A: Real-Time Voice Pipeline & Sandbox CRM Integration (Weeks 3–5)
- **Objective**: Connect real-time audio streaming (STT/TTS) and wire the CRM adapter to your staging CRM API.
- **Deliverables**:
  - Deploy [Pipecat](https://docs.pipecat.ai/overview/introduction) or [LiveKit Agents](https://docs.livekit.io/agents/start/voice-ai/) worker connected to **Azure AI Speech (`uaenorth`)** and **Azure OpenAI (`uaenorth`)**.
  - Implement concrete `RestCRMAdapter` (OAuth2 client credentials flow, retry with exponential backoff, circuit breaker).
  - Test voice barge-in, latency benchmarks (<800ms), and English accent recognition (including UAE/GCC English phrasing).

### Phase 1B: Genesys Cloud Integration & Controlled Pilot (Weeks 6–8)
- **Objective**: Wire the voice pipeline to Genesys and run a supervised pilot with internal staff and a small customer cohort.
- **Deliverables**:
  - Configure [Genesys Cloud Audio Connector](https://help.mypurecloud.com/articles/configure-and-activate-audio-connector-in-genesys-cloud/) in an Architect Outbound Flow.
  - Map `escalate_to_human_agent` to Genesys Architect Transfer-to-ACD action with Participant Data attributes for Agent Workspace Screen Pop.
  - Enable automated PII scrubbing, call recording consent logging, and TDRA DNCR pre-call checks.

### Phase 2: Outlook Email Automation & Omnichannel Memory (Weeks 9–12)
- **Objective**: Extend the orchestrator to read, classify, action, and reply to customer emails via Microsoft Outlook.
- **Deliverables**:
  - Build `OutlookGraphAdapter` using [Microsoft Graph Mail API](https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0).
  - Deploy a unified PostgreSQL + pgvector context store in UAE North so voice and email agents share a single 360-degree view of every customer interaction.

---

## 6. How to Run and Extend the Working Prototype

Alongside this architecture blueprint, I have built a self-contained, runnable Python prototype (`voice_ai_orchestrator_prototype.py`) that implements the full core architecture.

### What Is Included in `voice_ai_orchestrator_prototype.py`
1. **UAE Compliance Engine (`UAEComplianceGuard`)**: Enforces TDRA calling windows (09:00–18:00 `Asia/Dubai`), DNCR registry checks, daily/weekly dial attempt limits, and instant opt-out registration.
2. **Pluggable Backend Adapters (`BaseCRMAdapter`, `BaseTelephonyAdapter`, `BaseEmailAdapter`)**: Abstract interfaces plus working in-memory/REST-ready adapters for CRM customer/offer lookups, feedback logging, service/complaint ticket creation, Genesys warm transfer with screen-pop context, and Phase 2 Outlook email ingestion.
3. **LLM Tool-Calling Orchestrator (`OmnichannelAgentOrchestrator`)**: Full OpenAI/Azure OpenAI JSON tool-schema definitions and an execution router that works out-of-the-box with a deterministic local simulator **or** live Azure OpenAI (`uaenorth`) credentials.
4. **FastAPI WebSocket & Webhook Blueprint**: Production-ready endpoint structure for `/ws/genesys-audiohook`, `/calls/outbound`, and `/webhooks/outlook-email`.

### Running the Prototype Locally
```bash
# Run the end-to-end simulation (tests outbound call, offer pitch, complaint ticket creation, feedback logging, human transfer, and Phase 2 email ingestion)
python3 voice_ai_orchestrator_prototype.py

# To connect to live Azure OpenAI in UAE North:
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_API_KEY="your-key"
export AZURE_OPENAI_DEPLOYMENT="gpt-4o"
```
