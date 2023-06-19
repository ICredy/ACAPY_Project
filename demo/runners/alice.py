import asyncio
import base64
import binascii
import json
import logging
import os
import sys
from urllib.parse import urlparse

from agent_container import (  # noqa:E402
    arg_parser,
    create_agent_with_args,
    AriesAgent,
)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runners.support.utils import (  # noqa:E402
    check_requires,
    log_msg,
    log_status,
    log_timer,
    prompt,
    prompt_loop,
)

logging.basicConfig(level=logging.WARNING)
LOGGER = logging.getLogger(__name__)


class AliceAgent(AriesAgent):
    def __init__(
        self,
        ident: str,
        http_port: int,
        admin_port: int,
        no_auto: bool = False,
        aip: int = 20,
        endorser_role: str = None,
        **kwargs,
    ):
        super().__init__(
            ident,
            http_port,
            admin_port,
            prefix="Alice",
            no_auto=no_auto,
            seed=None,
            aip=aip,
            endorser_role=endorser_role,
            **kwargs,
        )
        self.connection_id = None
        self._connection_ready = None
        self.cred_state = {}

    async def detect_connection(self):
        await self._connection_ready
        self._connection_ready = None

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()


#Overall, the input_invitation function is responsible for extracting invitation details from user input, 
# decoding and validating the invitation, and passing the processed invitation to the
#  input_invitation method of the agent_container. 
async def input_invitation(agent_container):
    #This future is used to track the readiness of the connection.
    agent_container.agent._connection_ready = asyncio.Future()
    #This suggests that it prompts the user for invitation details repeatedly until a valid input is received.
    async for details in prompt_loop("Invite details: "):
        #code attempts to parse the provided details as a URL and extracts the invitation information
        b64_invite = None
        try:
            url = urlparse(details)
            query = url.query
            if query and "c_i=" in query:
                pos = query.index("c_i=") + 4
                b64_invite = query[pos:]
            elif query and "oob=" in query:
                pos = query.index("oob=") + 4
                b64_invite = query[pos:]
            else:
                b64_invite = details
        except ValueError:
            b64_invite = details

        if b64_invite:
            try:
                padlen = 4 - len(b64_invite) % 4
                if padlen <= 2:
                    b64_invite += "=" * padlen
                invite_json = base64.urlsafe_b64decode(b64_invite)
                details = invite_json.decode("utf-8")
            except binascii.Error:
                pass
            except UnicodeDecodeError:
                pass

#The decoded invitation is checked for validity. It attempts to parse the invitation as JSON using json.loads. If the parsing is successful, the loop is broken, indicating that a valid invitation has been obtained. If there is an error during parsing, an error message is logged.
        if details:
            try:
                details = json.loads(details)
                break
            except json.JSONDecodeError as e:
                log_msg("Invalid invitation:", str(e))

    with log_timer("Connect duration:"):
        connection = await agent_container.input_invitation(details, wait=True)


#Overall, the code sets up an agent, performs various operations based on user input through a prompt loop, 
# #logs status and timing information, and terminates the agent before exiting the script. 
async def main(args):
    alice_agent = await create_agent_with_args(args, ident="alice")

    try:
        log_status(
            "#7 Provision an agent and wallet, get back configuration details"
            + (
                f" (Wallet type: {alice_agent.wallet_type})"
                if alice_agent.wallet_type
                else ""
            )
        )
        agent = AliceAgent(
            "alice.agent",
            alice_agent.start_port,
            alice_agent.start_port + 1,
            genesis_data=alice_agent.genesis_txns,
            genesis_txn_list=alice_agent.genesis_txn_list,
            no_auto=alice_agent.no_auto,
            tails_server_base_url=alice_agent.tails_server_base_url,
            revocation=alice_agent.revocation,
            timing=alice_agent.show_timing,
            multitenant=alice_agent.multitenant,
            mediation=alice_agent.mediation,
            wallet_type=alice_agent.wallet_type,
            aip=alice_agent.aip,
            endorser_role=alice_agent.endorser_role,
        )

        await alice_agent.initialize(the_agent=agent)

        log_status("#9 Input faber.py invitation details")
        await input_invitation(alice_agent)

        options = "    (3) Send Message\n" "    (4) Input New Invitation\n"
        if alice_agent.endorser_role and alice_agent.endorser_role == "author":
            options += "    (D) Set Endorser's DID\n"
        if alice_agent.multitenant:
            options += "    (W) Create and/or Enable Wallet\n"
        options += "    (X) Exit?\n[3/4/{}X] ".format(
            "W/" if alice_agent.multitenant else "",
        )
        async for option in prompt_loop(options):
            if option is not None:
                option = option.strip()

            if option is None or option in "xX":
                break

            elif option in "dD" and alice_agent.endorser_role:
                endorser_did = await prompt("Enter Endorser's DID: ")
                await alice_agent.agent.admin_POST(
                    f"/transactions/{alice_agent.agent.connection_id}/set-endorser-info",
                    params={"endorser_did": endorser_did, "endorser_name": "endorser"},
                )

            elif option in "wW" and alice_agent.multitenant:
                target_wallet_name = await prompt("Enter wallet name: ")
                include_subwallet_webhook = await prompt(
                    "(Y/N) Create sub-wallet webhook target: "
                )
                if include_subwallet_webhook.lower() == "y":
                    await alice_agent.agent.register_or_switch_wallet(
                        target_wallet_name,
                        webhook_port=alice_agent.agent.get_new_webhook_port(),
                        mediator_agent=alice_agent.mediator_agent,
                        taa_accept=alice_agent.taa_accept,
                    )
                else:
                    await alice_agent.agent.register_or_switch_wallet(
                        target_wallet_name,
                        mediator_agent=alice_agent.mediator_agent,
                        taa_accept=alice_agent.taa_accept,
                    )

            elif option == "3":
                msg = await prompt("Enter message: ")
                if msg:
                    await alice_agent.agent.admin_POST(
                        f"/connections/{alice_agent.agent.connection_id}/send-message",
                        {"content": msg},
                    )

            elif option == "4":
                # handle new invitation
                log_status("Input new invitation details")
                await input_invitation(alice_agent)

        if alice_agent.show_timing:
            timing = await alice_agent.agent.fetch_timing()
            if timing:
                for line in alice_agent.agent.format_timing(timing):
                    log_msg(line)

    finally:
        terminated = await alice_agent.terminate()

#delay
    await asyncio.sleep(0.1)

#alice_agent was not terminated, the script exits with a status code of 1 (os._exit(1)).
    if not terminated:
        os._exit(1)


if __name__ == "__main__":
    parser = arg_parser(ident="alice", port=8030)
    args = parser.parse_args()

    ENABLE_PYDEVD_PYCHARM = os.getenv("ENABLE_PYDEVD_PYCHARM", "").lower()
    ENABLE_PYDEVD_PYCHARM = ENABLE_PYDEVD_PYCHARM and ENABLE_PYDEVD_PYCHARM not in (
        "false",
        "0",
    )
    PYDEVD_PYCHARM_HOST = os.getenv("PYDEVD_PYCHARM_HOST", "localhost")
    PYDEVD_PYCHARM_CONTROLLER_PORT = int(
        os.getenv("PYDEVD_PYCHARM_CONTROLLER_PORT", 5001)
    )

    if ENABLE_PYDEVD_PYCHARM:
        try:
            import pydevd_pycharm

            print(
                "Alice remote debugging to "
                f"{PYDEVD_PYCHARM_HOST}:{PYDEVD_PYCHARM_CONTROLLER_PORT}"
            )
            pydevd_pycharm.settrace(
                host=PYDEVD_PYCHARM_HOST,
                port=PYDEVD_PYCHARM_CONTROLLER_PORT,
                stdoutToServer=True,
                stderrToServer=True,
                suspend=False,
            )
        except ImportError:
            print("pydevd_pycharm library was not found")

    check_requires(args)

    try:
        asyncio.get_event_loop().run_until_complete(main(args))
    except KeyboardInterrupt:
        os._exit(1)
