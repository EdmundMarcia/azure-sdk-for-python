# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
import asyncio
import os
import shutil
import sys
from typing import Any, List, Optional

from azure.core.exceptions import ClientAuthenticationError
from azure.core.credentials import AccessToken
from .._internal import AsyncContextManager
from .._internal.decorators import log_get_token_async
from ... import CredentialUnavailableError
from ..._credentials.azure_cli import (
    AzureCliCredential as _SyncAzureCliCredential,
    CLI_NOT_FOUND,
    COMMAND_LINE,
    EXECUTABLE_NAME,
    get_safe_working_dir,
    NOT_LOGGED_IN,
    parse_token,
    sanitize_output,
)
from ..._internal import _scopes_to_resource, resolve_tenant, within_dac, validate_tenant_id, validate_scope


class AzureCliCredential(AsyncContextManager):
    """Authenticates by requesting a token from the Azure CLI.

    This requires previously logging in to Azure via "az login", and will use the CLI's currently logged in identity.

    :keyword str tenant_id: Optional tenant to include in the token request.
    :keyword List[str] additionally_allowed_tenants: Specifies tenants in addition to the specified "tenant_id"
        for which the credential may acquire tokens. Add the wildcard value "*" to allow the credential to
        acquire tokens for any tenant the application can access.
    :keyword int process_timeout: Seconds to wait for the Azure CLI process to respond. Defaults to 10 seconds.

    .. admonition:: Example:

        .. literalinclude:: ../samples/credential_creation_code_snippets.py
            :start-after: [START create_azure_cli_credential_async]
            :end-before: [END create_azure_cli_credential_async]
            :language: python
            :dedent: 4
            :caption: Create an AzureCliCredential.
    """

    def __init__(
        self,
        *,
        tenant_id: str = "",
        additionally_allowed_tenants: Optional[List[str]] = None,
        process_timeout: int = 10,
    ) -> None:
        if tenant_id:
            validate_tenant_id(tenant_id)
        self.tenant_id = tenant_id
        self._additionally_allowed_tenants = additionally_allowed_tenants or []
        self._process_timeout = process_timeout

    @log_get_token_async
    async def get_token(
        self,
        *scopes: str,
        claims: Optional[str] = None,  # pylint:disable=unused-argument
        tenant_id: Optional[str] = None,
        **kwargs: Any,
    ) -> AccessToken:
        """Request an access token for `scopes`.

        This method is called automatically by Azure SDK clients. Applications calling this method directly must
        also handle token caching because this credential doesn't cache the tokens it acquires.

        :param str scopes: desired scope for the access token. This credential allows only one scope per request.
            For more information about scopes, see
            https://learn.microsoft.com/azure/active-directory/develop/scopes-oidc.
        :keyword str claims: not used by this credential; any value provided will be ignored.
        :keyword str tenant_id: optional tenant to include in the token request.

        :return: An access token with the desired scopes.
        :rtype: ~azure.core.credentials.AccessToken
        :raises ~azure.identity.CredentialUnavailableError: the credential was unable to invoke the Azure CLI.
        :raises ~azure.core.exceptions.ClientAuthenticationError: the credential invoked the Azure CLI but didn't
          receive an access token.
        """
        # only ProactorEventLoop supports subprocesses on Windows (and it isn't the default loop on Python < 3.8)
        if sys.platform.startswith("win") and not isinstance(asyncio.get_event_loop(), asyncio.ProactorEventLoop):
            return _SyncAzureCliCredential().get_token(*scopes, tenant_id=tenant_id, **kwargs)

        if tenant_id:
            validate_tenant_id(tenant_id)
        for scope in scopes:
            validate_scope(scope)

        resource = _scopes_to_resource(*scopes)
        command = COMMAND_LINE.format(resource)
        tenant = resolve_tenant(
            default_tenant=self.tenant_id,
            tenant_id=tenant_id,
            additionally_allowed_tenants=self._additionally_allowed_tenants,
            **kwargs,
        )

        if tenant:
            command += " --tenant " + tenant
        output = await _run_command(command, self._process_timeout)

        token = parse_token(output)
        if not token:
            sanitized_output = sanitize_output(output)
            message = (
                f"Unexpected output from Azure CLI: '{sanitized_output}'. \n"
                f"To mitigate this issue, please refer to the troubleshooting guidelines here at "
                f"https://aka.ms/azsdk/python/identity/azclicredential/troubleshoot."
            )
            if within_dac.get():
                raise CredentialUnavailableError(message=message)
            raise ClientAuthenticationError(message=message)

        return token

    async def close(self) -> None:
        """Calling this method is unnecessary"""


async def _run_command(command: str, timeout: int) -> str:
    # Ensure executable exists in PATH first. This avoids a subprocess call that would fail anyway.
    if shutil.which(EXECUTABLE_NAME) is None:
        raise CredentialUnavailableError(message=CLI_NOT_FOUND)

    if sys.platform.startswith("win"):
        args = ("cmd", "/c " + command)
    else:
        args = ("/bin/sh", "-c", command)

    working_directory = get_safe_working_dir()

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=working_directory,
            env=dict(os.environ, AZURE_CORE_NO_COLOR="true"),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        output = stdout_b.decode()
        stderr = stderr_b.decode()
    except asyncio.TimeoutError as ex:
        proc.kill()
        raise CredentialUnavailableError(message="Timed out waiting for Azure CLI") from ex
    except OSError as ex:
        # failed to execute 'cmd' or '/bin/sh'
        error = CredentialUnavailableError(message="Failed to execute '{}'".format(args[0]))
        raise error from ex

    if proc.returncode == 0:
        return output

    # Fallback check in case the executable is not found while executing subprocess.
    if proc.returncode == 127 or stderr.startswith("'az' is not recognized"):
        raise CredentialUnavailableError(CLI_NOT_FOUND)

    if ("az login" in stderr or "az account set" in stderr) and "AADSTS" not in stderr:
        raise CredentialUnavailableError(message=NOT_LOGGED_IN)

    message = sanitize_output(stderr) if stderr else "Failed to invoke Azure CLI"
    if within_dac.get():
        raise CredentialUnavailableError(message=message)
    raise ClientAuthenticationError(message=message)
