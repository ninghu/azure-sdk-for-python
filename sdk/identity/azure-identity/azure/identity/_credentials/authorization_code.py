# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
from typing import Optional, Any

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from .._internal.aad_client import AadClient
from .._internal.get_token_mixin import GetTokenMixin


class AuthorizationCodeCredential(GetTokenMixin):
    """Authenticates by redeeming an authorization code previously obtained from Microsoft Entra ID.

    See `Microsoft Entra ID documentation
    <https://docs.microsoft.com/azure/active-directory/develop/v2-oauth2-auth-code-flow>`__ for more information
    about the authentication flow.

    :param str tenant_id: ID of the application's Microsoft Entra tenant. Also called its "directory" ID.
    :param str client_id: The application's client ID
    :param str authorization_code: The authorization code from the user's log-in
    :param str redirect_uri: The application's redirect URI. Must match the URI used to request the authorization code.

    :keyword str authority: Authority of a Microsoft Entra endpoint, for example "login.microsoftonline.com",
        the authority for Azure Public Cloud (which is the default). :class:`~azure.identity.AzureAuthorityHosts`
        defines authorities for other clouds.
    :keyword str client_secret: One of the application's client secrets. Required only for web apps and web APIs.
    :keyword List[str] additionally_allowed_tenants: Specifies tenants in addition to the specified "tenant_id"
        for which the credential may acquire tokens. Add the wildcard value "*" to allow the credential to
        acquire tokens for any tenant the application can access.

    .. admonition:: Example:

        .. literalinclude:: ../samples/credential_creation_code_snippets.py
            :start-after: [START create_authorization_code_credential]
            :end-before: [END create_authorization_code_credential]
            :language: python
            :dedent: 4
            :caption: Create an AuthorizationCodeCredential.
    """

    def __init__(
        self, tenant_id: str, client_id: str, authorization_code: str, redirect_uri: str, **kwargs: Any
    ) -> None:
        self._authorization_code: Optional[str] = authorization_code
        self._client_id = client_id
        self._client_secret = kwargs.pop("client_secret", None)
        self._client = kwargs.pop("client", None) or AadClient(tenant_id, client_id, **kwargs)
        self._redirect_uri = redirect_uri
        super(AuthorizationCodeCredential, self).__init__()

    def __enter__(self) -> "AuthorizationCodeCredential":
        self._client.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._client.__exit__(*args)

    def close(self) -> None:
        """Close the credential's transport session."""
        self.__exit__()

    def get_token(
        self, *scopes: str, claims: Optional[str] = None, tenant_id: Optional[str] = None, **kwargs: Any
    ) -> AccessToken:
        """Request an access token for `scopes`.

        This method is called automatically by Azure SDK clients.

        The first time this method is called, the credential will redeem its authorization code. On subsequent calls
        the credential will return a cached access token or redeem a refresh token, if it acquired a refresh token upon
        redeeming the authorization code.

        :param str scopes: desired scopes for the access token. This method requires at least one scope.
            For more information about scopes, see
            https://learn.microsoft.com/azure/active-directory/develop/scopes-oidc.
        :keyword str claims: additional claims required in the token, such as those returned in a resource provider's
            claims challenge following an authorization failure.
        :keyword str tenant_id: optional tenant to include in the token request.

        :return: An access token with the desired scopes.
        :rtype: ~azure.core.credentials.AccessToken
        :raises ~azure.core.exceptions.ClientAuthenticationError: authentication failed. The error's ``message``
          attribute gives a reason. Any error response from Microsoft Entra ID is available as the error's
          ``response`` attribute.
        """
        # pylint:disable=useless-super-delegation
        return super(AuthorizationCodeCredential, self).get_token(*scopes, claims=claims, tenant_id=tenant_id, **kwargs)

    def _acquire_token_silently(self, *scopes: str, **kwargs) -> Optional[AccessToken]:
        return self._client.get_cached_access_token(scopes, **kwargs)

    def _request_token(self, *scopes: str, **kwargs) -> AccessToken:
        if self._authorization_code:
            token = self._client.obtain_token_by_authorization_code(
                scopes=scopes, code=self._authorization_code, redirect_uri=self._redirect_uri, **kwargs
            )
            self._authorization_code = None  # auth codes are single-use
            return token

        token = None
        for refresh_token in self._client.get_cached_refresh_tokens(scopes):
            if "secret" in refresh_token:
                token = self._client.obtain_token_by_refresh_token(scopes, refresh_token["secret"], **kwargs)
                if token:
                    break

        if not token:
            raise ClientAuthenticationError(
                message="No authorization code, cached access token, or refresh token available."
            )

        return token
