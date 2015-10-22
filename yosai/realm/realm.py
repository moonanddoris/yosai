"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""

from yosai import (
    AccountStoreRealmAuthenticationException,
    CacheCredentialsException,
    ClearCacheCredentialsException,
    GetCachedCredentialsException,
    IllegalArgumentException,
    IndexedAuthorizationInfo,
    IncorrectCredentialsException,
    IndexedPermissionVerifier,
    LogManager,
    PasswordVerifier,
    RealmMisconfiguredException,
    SimpleRoleVerifier,
    UsernamePasswordToken,
    authz_abcs,
    realm_abcs,
)

class AccountStoreRealm(realm_abcs.AuthenticatingRealm,
                        realm_abcs.AuthorizingRealm,
                        authz_abcs.PermissionResolverAware):
    """
    A Realm interprets information from a datastore.

    Differences between yosai and shiro include:
        1) yosai uses two AccountStoreRealm interfaces to specify authentication
           and authorization
        2) yosai includes support for authorization within the AccountStoreRealm
            - as of shiro v2 alpha rev1693638, shiro doesn't (yet)
        3) yosai renamed account_cache objects to credentials_cache objects

    """

    def __init__(self):
        #  DG:  this needs to be updated so that positional arguments
        #       are used to construct the object rather than mutator methods

        self.name = 'AccountStoreRealm' + str(id(self))  # DG:  replace later..
        self._account_store = None  # DG:  TBD
        self._credentials_cache_handler = None  # DG:  TBD
        self._authorization_cache_handler = None  # DG:  TBD

        #yosai renamed credentials_matcher:
        self._credentials_verifier = PasswordVerifier()  # 80/20 rule: passwords

        self._permission_verifier = IndexedPermissionVerifier()
        self._role_verifier = SimpleRoleVerifier()

    @property
    def account_store(self):
        return self._account_store

    @account_store.setter
    def account_store(self, accountstore):
        self._account_store = accountstore

    @property
    def credentials_verifier(self):
        return self._credentials_verifier

    @credentials_verifier.setter
    def credentials_verifier(self, credentialsmatcher):
        self._credentials_verifier = credentialsmatcher

    @property
    def credentials_cache_handler(self):
        return self._credentials_cache_handler

    @credentials_cache_handler.setter
    def credentials_cache_handler(self, credentialscachehandler):
        self._credentials_cache_handler = credentialscachehandler

    @property
    def authorization_cache_handler(self):
        return self._authorization_cache_handler

    @authorization_cache_handler.setter
    def authorization_cache_handler(self, authorizationcachehandler):
        self._authorization_cache_handler = authorizationcachehandler

    @property
    def permission_resolver(self):
        try:
            return self.permission_verifier.permission_resolver
        except AttributeError:
            self.permission_verifier.permission_resolver = None
            return self.permission_verifier.permission_resolver

    @permission_resolver.setter
    def permission_resolver(self, permissionresolver):
        # passes through realm and onto the verifier that actually uses it
        self.permission_verifier.permission_resolver = permissionresolver

    @property
    def permission_verifier(self):
        return self._permission_verifier

    @permission_verifier.setter
    def permission_verifier(self, verifier):
        self._permission_verifier = verifier

    @property
    def role_verifier(self):
        return self._role_verifier

    @role_verifier.setter
    def role_verifier(self, verifier):
        self._role_verifier = verifier

    def do_clear_cache(self, identifiers):
        msg = "Clearing cache for: " + str(identifiers)
        print(msg)
        # log info here

        self.clear_cached_credentials(identifiers)
        self.clear_cached_authorization_info(identifiers)

    def clear_cached_credentials(self, identifiers):
        """
        When cached credentials are no longer needed, they can be manually
        cleared with this method.  However, account credentials should be
        cached with a short expiration time (TTL), making the manual clearing
        of cached credentials an alternative use case.
        """

        self.credentials_cache_handler.clear_cached_credentials(identifiers)

    def clear_cached_authorization_info(self, identifiers):
        """
        This process prevents stale authorization data from being used.
        If any authorization data for an account is changed at runtime, such as
        adding or removing roles and/or permissions, the subclass implementation
        of AccountStoreRealm should clear the cached AuthorizationInfo for that
        account through this method. This ensures that the next call to
        get_authorization_info(PrincipalCollection) will acquire the account's
        fresh authorization data, which is cached for efficient re-use.
        """
        self.authorization_cache_handler.clear_cached_authz_info(identifiers)

    # --------------------------------------------------------------------------
    # Authentication
    # --------------------------------------------------------------------------

    # removed the basic accessor/mutator methods (not pythonic)
    def supports(self, authc_token):
        # override the following return to False if you do not wish to support
        # authentication from this realm
        return isinstance(authc_token, UsernamePasswordToken)

    # new to yosai (refactor):
    def get_credentials(self, authc_token):
        """ The default authentication caching policy is to cache an account's
            credentials that are queried from an account store, for a specific
            user, so to facilitate any subsequent authentication attempts for
            that user. Naturally, in order to cache one must have a CacheHandler.
            If a user were to fail to authenticate, perhaps due to an
            incorrectly entered password, during the the next authentication
            attempt (of that user id) the cached account will be readily
            available from cache and used to match credentials, boosting
            performance.

        :returns: an Account object
        """
        account = None
        cch = self.credentials_cache_handler
        if cch:
            account = cch.get_cached_credentials(authc_token)
        if (not account):
            # account not cached, so retrieve it from the account_store
            try:
                account = self.account_store.get_account(authc_token)
            except AttributeError:
                msg = ('AccountStoreRealm misconfigured.  At a minimum, '
                       'define an AccountStore. Further, define a'
                       ' CacheHandler to cache an authenticated account')
                # log here (exception)
                raise RealmMisconfiguredException(msg)
            if (authc_token and account):
                msg = ("Acquired Account [{0}] from account store".format(
                       account))
                # log here (debug)
                print(msg)

                # DG:  caches pre-authenticated values
                if cch:
                    # Note:  credentials are set with a short TTL in cache
                    cch.cache_credentials(authc_token, account)

        else:
            msg2 = ("Using cached account [{0}] for credentials "
                    "matching.".format(account))
            # log here (debug)
            print(msg2)

        if (not account):
            # log here
            msg3 = ("No account found for submitted AuthenticationToken "
                    "[{0}].  Returning None.".format(authc_token))
            print(msg3)

        return account

    # yosai refactors:
    def authenticate_account(self, authc_token):

        account = self.get_credentials(authc_token)
        self.assert_credentials_match(authc_token, account)

        # at this point, authentication is confirmed, so clear
        # the cache of credentials (however, they should have a short ttl anyway)
        self.clear_cached_credentials(authc_token.identifier)
        return account

    def assert_credentials_match(self, authc_token, account):
        cm = self.credentials_verifier
        if (not cm.credentials_match(authc_token, account)):
            # not successful - raise an exception as signal:
            msg = ("Submitted credentials for token [" + str(authc_token) +
                   "] did not match the stored credentials.")
            # log here
            raise IncorrectCredentialsException(msg)

    # --------------------------------------------------------------------------
    # Authorization
    # --------------------------------------------------------------------------

    def get_authorization_info(self, identifiers):
        """
        The default caching policy is to cache an account's authorization info,
        obtained from an account store, for a specific user, so to facilitate
        any subsequent authorization checks for that user. Naturally, in order
        to cache one must have a CacheHandler.

        :returns: an AuthorizationInfo object
        """
        authz_info = None

        msg = ("Retrieving AuthorizationInfo for identifiers [{0}]".
               format(identifiers))
        # log trace here
        print(msg)

        ach = self.authorization_cache_handler

        if (ach):
            msg = "Attempting to retrieve the AuthorizationInfo from cache."
            # log trace here
            print(msg)

            # new to yosai:
            authz_info = ach.get_cached_authz_info(identifiers)

            # TBD -- log only if logging level is TRACE:
            if (authz_info is None):
                msg = ("AuthorizationInfo NOT found in cache for identifiers ["
                       + identifiers + "]")
                # log trace here
                print(msg)
            else:
                msg = ("AuthorizationInfo found in cache for identifiers ["
                       + identifiers + "]")
                # log trace here
                print(msg)

        if (authz_info is None):
            # new to yosai:
            account = self.account_store.get_authz_info(identifiers)

            try:
                authz_info = IndexedAuthorizationInfo(roles=account.roles,
                                                      permissions=account.permissions)
            except AttributeError:
                msg = "Could not obtain Account authorization info from store."
                print(msg)
                # log warning
                authz_info = None

            # If the info is not None and cache exists, then cache the
            # authorization info
            if (authz_info and ach):

                msg = ("Caching authorization info for identifiers: [" +
                       identifiers + "].")
                # log trace here
                print(msg)

                ach.cache_authz_info(identifiers, authz_info)

        return authz_info

    def is_permitted(self, identifiers, permission_s):
        """
        :param permission_s: a collection of one or more permissions, represented
                             as string-based permissions or Permission objects
                             and NEVER comingled
        :type permission_s: list of either String(s) or Permission(s)
        :yields: tuple(Permission, Boolean)
        """

        authz_info = self.get_authorization_info(identifiers)
        yield from self.permission_verifier.is_permitted(authz_info,
                                                         permission_s)

    def has_role(self, identifiers, roleid_s):
        """
        Confirms whether a subject is a member of one or more roles.

        :param roleid_s: a collection of 1..N Role identifiers
        :type roleid_s: Set of String(s)

        :yields: tuple(roleid, Boolean)
        """
        authz_info = self.get_authorization_info(identifiers)
        yield from self.role_verifier.has_role(authz_info, roleid_s)

# omitted AbstractCacheHandler implementation / references

class DefaultCredentialsCacheHandler(realm_abcs.CredentialsCacheHandler):

    def __init__(self, cache_resolver, cache_key_resolver):
        # this init is new to Yosai in that it requires 2 positional arguments
        self.credentials_cache_key_resolver = cache_key_resolver
        self.credentials_cache_resolver = cache_resolver
        self.cache_manager = None  # rather thn AbstractCacheManager dependency

    # omitted accessor / mutator methods for attributes (not pythonic)

    def get_cached_credentials(self, authc_token):
        try:
            cache = self.credentials_cache_resolver.\
                get_cache(authc_token=authc_token)
            key = self.credentials_cache_key_resolver.\
                get_cache_key(authc_token=authc_token)
            # log here
            return cache.get(key)
        except AttributeError:
            raise GetCachedCredentialsException

    def cache_credentials(self, authc_token, account):
        try:
            cache = self.credentials_cache_resolver.\
                get_cache(authc_token=authc_token, account=account)
            key = self.credentials_cache_key_resolver.\
                get_cache_key(authc_token=authc_token, account=account)
            if not key:  # a key is required to cache, so this is an issue
                raise CacheCredentialsException
            # log here
            cache.put(key, account)
        except AttributeError:
            raise CacheCredentialsException

    def clear_cached_credentials(self, account_id):
        try:
            cache = self.credentials_cache_resolver.\
                get_cache(account_id=account_id)
            key = self.credentials_cache_key_resolver.\
                get_cache_key(account_id=account_id)

            # None implies that either it doesn't exist in cache or there's a
            # problem in locating it in cache.  The latter is harder to verify
            # so just log a trail to debug (in case).
            if (not key):
                # log here
                if not cache:
                    # log here
                    raise ClearCacheCredentialsException
                return None
            return cache.remove(key)
        except AttributeError:
            raise ClearCacheCredentialsException
