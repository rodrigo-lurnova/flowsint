import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx

from flowsint_core.core.enricher_base import Enricher
from flowsint_core.core.logger import Logger
from flowsint_enrichers.registry import flowsint_enricher
from flowsint_types import Email, Username
from flowsint_types.social_account import SocialAccount


@flowsint_enricher
class EmailToHoleheEnricher(Enricher):
    """[Holehe] Discover which websites/services an email address is registered on.

    Unlike the breach/leak enrichers, holehe does *account existence* discovery:
    it queries ~120 sites (via their password-reset / signup flows) and reports
    where the email has an account, plus any partially masked recovery email or
    phone the site leaks. This is information no other email enricher surfaces.
    """

    InputType = Email
    OutputType = SocialAccount

    # Per-site HTTP timeout and an overall budget per email so one slow module
    # can't stall the whole batch.
    _MODULE_TIMEOUT = 10
    _EMAIL_BUDGET = 120

    @classmethod
    def name(cls) -> str:
        return "email_to_holehe"

    @classmethod
    def category(cls) -> str:
        return "Email"

    @classmethod
    def key(cls) -> str:
        return "email"

    @staticmethod
    def _load_modules() -> List[Any]:
        """Import every holehe module and return its site coroutine functions.

        Done lazily (inside scan) so enricher auto-discovery doesn't pay the
        cost of importing ~120 submodules at startup.
        """
        from holehe.core import get_functions, import_submodules

        modules = import_submodules("holehe.modules")
        # get_functions expects an argparse-like namespace; nopasswordrecovery
        # drops adobe/mail_ru/odnoklassniki/samsung (slow / rate-limited).
        args = SimpleNamespace(nopasswordrecovery=False)
        return get_functions(modules, args)

    @staticmethod
    def _parse_holehe_output(
        email_value: str, out: List[Dict[str, Any]]
    ) -> List[SocialAccount]:
        """Turn holehe's raw result dicts into SocialAccount entities.

        Each dict holehe appends looks like::

            {"name": "twitter", "domain": "twitter.com", "rateLimit": False,
             "exists": True, "emailrecovery": "ex****e@gmail.com",
             "phoneNumber": "0*******78", "others": None}

        Only entries with ``exists is True`` represent a real account. Pure and
        side-effect free so it can be unit-tested without the network.

        The email is used as the account identity (``username``) because that's
        exactly what holehe proves — this email has an account here — which also
        gives SocialAccount a unique ``id`` of ``email@platform`` (its
        ``compute_label_and_id`` validator derives the id from username+platform),
        so two different emails on the same service don't collapse into one node.
        """
        results: List[SocialAccount] = []
        for entry in out:
            if not isinstance(entry, dict):
                continue
            if entry.get("exists") is not True:
                continue

            name = entry.get("name") or entry.get("domain")
            if not name:
                continue
            platform = entry.get("domain") or name

            # Surface the recovery hints holehe leaks (masked email/phone) in the
            # bio so the analyst sees them without inventing new SocialAccount
            # fields they don't belong in.
            hints = []
            if entry.get("emailrecovery"):
                hints.append(f"recovery email: {entry['emailrecovery']}")
            if entry.get("phoneNumber"):
                hints.append(f"recovery phone: {entry['phoneNumber']}")
            bio = "; ".join(hints) if hints else None

            try:
                results.append(
                    SocialAccount(
                        username=Username(value=email_value),
                        nodeLabel=f"{email_value} @ {platform}",
                        display_name=name,
                        platform=platform,
                        associated_emails=[email_value],
                        bio=bio,
                    )
                )
            except Exception:
                continue
        return results

    async def _run_for_email(self, email_value: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            websites = self._load_modules()
        except Exception as e:
            Logger.error(
                self.sketch_id,
                {"message": f"(Holehe) failed to load modules: {e}"},
            )
            return out

        async with httpx.AsyncClient(timeout=self._MODULE_TIMEOUT) as client:

            async def _guarded(fn: Any) -> None:
                # holehe modules mutate `out` in place; a single failing module
                # must not abort the gather.
                try:
                    await fn(email_value, client, out)
                except Exception:
                    return

            tasks = [_guarded(fn) for fn in websites]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self._EMAIL_BUDGET,
                )
            except asyncio.TimeoutError:
                Logger.warn(
                    self.sketch_id,
                    {
                        "message": f"(Holehe) timed out for {email_value}; "
                        f"returning {len(out)} partial results"
                    },
                )
        return out

    async def scan(self, data: List[InputType]) -> List[OutputType]:
        results: List[OutputType] = []
        # Keep (source email, account) pairs so postprocess wires the right
        # relationship even when several emails are scanned in one batch —
        # more robust than matching on the derived node id.
        self._pairs: List[tuple] = []

        for email_obj in data:
            email_value = email_obj.email
            if not email_value:
                continue
            try:
                out = await self._run_for_email(email_value)
                accounts = self._parse_holehe_output(email_value, out)
                for account in accounts:
                    self._pairs.append((email_obj, account))
                results.extend(accounts)
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"(Holehe) exception while querying {email_value}: {e}"},
                )
                continue
        return results

    def postprocess(
        self, results: List[OutputType], original_input: List[InputType]
    ) -> List[OutputType]:
        if not self._graph_service:
            return results

        for source_email, account in getattr(self, "_pairs", []):
            try:
                self.create_node(source_email)
                self.create_node(account)
                self.create_relationship(
                    source_email, account, "HAS_SOCIAL_ACCOUNT"
                )
                self.log_graph_message(
                    f"(Holehe) {source_email.email} -> account on {account.platform}"
                )
            except Exception as e:
                Logger.error(
                    self.sketch_id,
                    {"message": f"(Holehe) failed to create graph nodes: {e}"},
                )
                continue
        return results


# Make types available at module level for easy access
InputType = EmailToHoleheEnricher.InputType
OutputType = EmailToHoleheEnricher.OutputType
