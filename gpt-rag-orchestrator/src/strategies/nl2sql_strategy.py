import asyncio
import logging
import re
from typing import AsyncIterator

logger = logging.getLogger(__name__)

from semantic_kernel.agents import (
    AzureAIAgent,
    AzureAIAgentSettings,
    AgentGroupChat
)
from semantic_kernel.agents.strategies import TerminationStrategy

from .base_agent_strategy import BaseAgentStrategy
from .agent_strategies import AgentStrategies
from plugins.nl2sql.plugin import NL2SQLPlugin


class ApprovalTerminationStrategy(TerminationStrategy):
    """Terminate as soon as the assistant emits TERMINATE."""
    def __init__(self, terminator_re: re.Pattern, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._terminator_re = terminator_re

    async def should_agent_terminate(self, agent, history):
        last = history[-1].content
        return bool(self._terminator_re.search(last))


class NL2SQLStrategy(BaseAgentStrategy):
    """
    An optimized NL2SQL Retrieval-Augmented Generation strategy
    """
    def __init__(self):
        super().__init__()
        self.strategy_type = AgentStrategies.NL2SQL

        # single plugin instance
        self._nl2sql_plugin = NL2SQLPlugin()

        # precompile the terminator-cleanup regex
        self._terminator_re = re.compile(r'\bterminate\b', re.IGNORECASE)

        # placeholders for prompts (lazy-loaded)
        self._triage_prompt    = None
        self._sqlquery_prompt  = None

    async def _load_prompts(self):
        """Load and cache the three prompt templates once per instance."""
        if self._triage_prompt is None:
            logger.debug("[NL2SQL] Loading prompt templates...")
            try:
                self._triage_prompt      = await self._read_prompt("triage_agent")
                self._sqlquery_prompt    = await self._read_prompt("sqlquery_agent")
                self._syntetizer_prompt  = await self._read_prompt("syntetizer_agent")
                logger.debug("[NL2SQL] Prompt templates loaded successfully.")
            except Exception as e:
                logger.error("[NL2SQL] Failed to load prompt templates: %s", e, exc_info=True)
                raise

    async def initiate_agent_flow(self, user_message: str) -> AsyncIterator[str]:
        # Prepend logged-in user context so agents can resolve self-referential queries
        # (e.g. "give me my reportees") without requiring the user to type their own email.
        principal_name = (self.user_context or {}).get("principal_name", "")
        if principal_name and principal_name != "anonymous":
            logger.info("[NL2SQL] Injecting logged-in user context for principal: %s", principal_name)
            user_message = (
                f"[Context: the logged-in user's email is {principal_name}. "
                f"When the user refers to 'me', 'my', 'I', or 'myself', use this email as the identity.]\n\n"
                f"{user_message}"
            )
        else:
            logger.debug("[NL2SQL] No authenticated principal found; proceeding without user context injection.")

        # ensure prompts are loaded
        await self._load_prompts()

        # prepare model settings
        ai_agent_settings = AzureAIAgentSettings(
            model_deployment_name=self.model_name,
            endpoint=self.project_endpoint
        )

        logger.info(
            "[NL2SQL] Starting agent flow: model=%s endpoint=%s principal=%s",
            self.model_name, self.project_endpoint, principal_name or "anonymous",
        )

        # open a single client/session for creation + streaming
        async with self.credential as creds, \
                   AzureAIAgent.create_client(
                       credential=creds,
                       endpoint=self.project_endpoint
                   ) as client:

            # 1) create all three agents in parallel
            logger.debug("[NL2SQL] Creating TriageAgent, SQLQueryAgent and SyntetizerAgent...")
            try:
                triage_def, sql_def, syntetizer_def = await asyncio.gather(
                    client.agents.create_agent(
                        model=ai_agent_settings.model_deployment_name,
                        name="TriageAgent",
                        instructions=self._triage_prompt
                    ),
                    client.agents.create_agent(
                        model=ai_agent_settings.model_deployment_name,
                        name="SQLQueryAgent",
                        instructions=self._sqlquery_prompt
                    ),
                    client.agents.create_agent(
                        model=ai_agent_settings.model_deployment_name,
                        name="SyntetizerAgent",
                        instructions=self._syntetizer_prompt
                    ),
                )
            except Exception as e:
                logger.error("[NL2SQL] Failed to create agents: %s", e, exc_info=True)
                raise
            logger.debug(
                "[NL2SQL] Agents created: triage_id=%s sql_id=%s syntetizer_id=%s",
                getattr(triage_def, "id", None),
                getattr(sql_def, "id", None),
                getattr(syntetizer_def, "id", None),
            )

            # 2) wrap them in AzureAIAgent objects (using keyword args!)
            triage_agent = AzureAIAgent(
                client=client,
                definition=triage_def,
                plugins=[self._nl2sql_plugin]
            )
            sqlquery_agent = AzureAIAgent(
                client=client,
                definition=sql_def,
                plugins=[self._nl2sql_plugin]
            )
            syntetizer_agent = AzureAIAgent(
                client=client,
                definition=syntetizer_def,
                plugins=[self._nl2sql_plugin]
            )

            # 3) assemble group chat with our custom terminator
            chat = AgentGroupChat(
                agents=[triage_agent, sqlquery_agent, syntetizer_agent],
                termination_strategy=ApprovalTerminationStrategy(
                    terminator_re=self._terminator_re,
                    agents=[syntetizer_agent],
                    maximum_iterations=10
                ),
            )

            try:
                # start the conversation
                logger.debug("[NL2SQL] Adding user message to group chat...")
                await chat.add_chat_message(message=user_message)

                buffer = ""
                chunk_count = 0
                async for content in chat.invoke_stream():
                    logger.debug(
                        "[NL2SQL] Stream chunk received: agent=%s content_len=%d",
                        content.name, len(content.content or ""),
                    )

                    if content.name == "SyntetizerAgent":
                        chunk_count += 1
                        buffer += content.content

                        # only process once the regex matches
                        if not self._terminator_re.search(buffer):
                            continue

                        # strip the terminator and yield
                        cleaned = self._terminator_re.sub("", buffer)
                        logger.info(
                            "[NL2SQL] SyntetizerAgent produced final answer (chunk #%d, len=%d).",
                            chunk_count, len(cleaned),
                        )
                        buffer = ""
                        yield cleaned

            except Exception as e:
                logger.error("[NL2SQL] Error during group chat invocation: %s", e, exc_info=True)
                raise
            finally:
                # clear conversation state
                try:
                    await chat.reset()
                    logger.debug("[NL2SQL] Chat reset completed.")
                except Exception as e:
                    logger.warning("[NL2SQL] Chat reset failed: %s", e, exc_info=True)

                # schedule background deletions
                for agent, name in [
                    (triage_agent, "triage_agent"),
                    (sqlquery_agent, "sqlquery_agent"),
                    (syntetizer_agent, "syntetizer_agent"),
                ]:
                    agent_id = getattr(agent, "id", None)
                    if agent_id:
                        logger.info("[NL2SQL] Scheduling deletion for %s (id=%s)", name, agent_id)
                        self._schedule_agent_deletion(agent_id)
                    else:
                        logger.warning("[NL2SQL] %s has no id; skipping deletion.", name)

    def _schedule_agent_deletion(self, agent_id: str):
        """
        Fire-and-forget deletion that opens its own client/session,
        preventing “Session is closed” errors.
        """
        async def _delete():
            try:
                async with self.credential as creds, \
                           AzureAIAgent.create_client(
                               credential=creds,
                               endpoint=self.project_endpoint
                           ) as delete_client:
                    await delete_client.agents.delete_agent(agent_id)
                    logger.info("[NL2SQL] Background deleted agent %s", agent_id)
            except Exception as e:
                logger.error("[NL2SQL] Failed background deletion of agent %s: %s", agent_id, e, exc_info=True)

        task = asyncio.create_task(_delete())
        task.add_done_callback(lambda t: t.exception())
