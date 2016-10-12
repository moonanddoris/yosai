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
from collections import defaultdict
import logging

from yosai.core import (
    AdditionalAuthenticationRequired,
    AuthenticationException,
    AuthenticationEventException,
    AuthenticationSettings,
    IncorrectCredentialsException,
    InvalidAuthenticationSequenceException,
    InvalidTokenException,
    LockedAccountException,
    UnknownAccountException,
    UnsupportedTokenException,
    authc_abcs,
    serialize_abcs,
    FirstRealmSuccessfulStrategy,
    DefaultAuthenticationAttempt,
    realm_abcs,
)

logger = logging.getLogger(__name__)


class UsernamePasswordToken(authc_abcs.AuthenticationToken):

    TIER = 1

    def __init__(self, username, password, remember_me=False, host=None):
        """
        :param username: the username submitted for authentication
        :type username: str

        :param password: the credentials submitted for authentication
        :type password: bytearray or string

        :param remember_me:  if the user wishes their identity to be
                             remembered across sessions
        :type remember_me: bool
        :param host:     the host name or IP string from where the attempt
                         is occuring
        :type host: str
        """
        self.identifier = username
        self.credentials = password
        self.host = host
        self.is_remember_me = remember_me

    @property
    def identifier(self):
        return self._identifier

    @identifier.setter
    def identifier(self, identifier):
        if not identifier:
            raise InvalidTokenException('Username must be defined')

        self._identifier = identifier

    @property
    def credentials(self):
        return self._credentials

    @credentials.setter
    def credentials(self, credentials):
        if isinstance(credentials, bytes):
            self._credentials = credentials
        if isinstance(credentials, str):
            self._credentials = bytes(credentials, 'utf-8')
        else:
            raise InvalidTokenException('Password must be a str or bytes')

    def clear(self):
        self._identifier = None
        self._host = None

        try:
            if (self._credentials):
                for index in range(len(self._credentials)):
                    self._credentials[index] = 0  # DG:  this equals 0x00
        except TypeError:
            msg = 'expected credentials to be a bytearray'
            raise InvalidTokenException(msg)

    def __repr__(self):
        result = "{0} - {1}, remember_me={2}".format(
            self.__class__.__name__, self.identifier, self.is_remember_me)
        if (self.host):
            result += ", ({0})".format(self.host)
        return result


class TOTPToken(authc_abcs.AuthenticationToken):

    TIER = 2

    def __init__(self, totp_token):
        """
        :param totp_key: the 6-digit token generated by the client, keyed using
                         the client's private key
        :type totp_key: int
        """
        self.credentials = totp_token

    @property
    def credentials(self):
        return self._credentials

    @credentials.setter
    def credentials(self, credentials):
        try:
            assert credentials >= 100000 and credentials < 1000000
            self._credentials = credentials
        except (TypeError, AssertionError):
            raise InvalidTokenException('TOTPToken must be a 6-digit int')


class DefaultAuthenticator(authc_abcs.Authenticator):

    # Unlike Shiro, Yosai injects the strategy and the eventbus
    def __init__(self,
                 settings,
                 strategy=FirstRealmSuccessfulStrategy()):

        self.authc_settings = AuthenticationSettings(settings)
        self.authentication_strategy = strategy

        if self.authc_settings.mfa_challenger:
            self.mfa_challenger = self.authc_settings.mfa_challenger()

        self.realms = None
        self.token_realm_resolver = None
        self.locking_realm = None
        self.locking_limit = None
        self.event_bus = None

    def init_realms(self, realms):
        """
        :type realms: Tuple
        """
        self.realms = tuple(realm for realm in realms
                             if isinstance(realm, realm_abcs.AuthenticatingRealm))
        self.register_cache_clear_listener()
        self.token_realm_resolver = self.init_token_resolution()
        self.init_locking()

    def init_locking(self):
        locking_limit = self.authc_settings.account_lock_threshold
        if locking_limit:
            self.locking_realm = self.locate_locking_realm()  # for account locking
            self.locking_limit = locking_limit

    def init_token_resolution(self):
        token_resolver = defaultdict(list)
        for realm in self.realms:
            if isinstance(realm, realm_abcs.AuthenticatingRealm):
                for token_class in realm.supported_authc_tokens:
                    token_resolver[token_class].append(realm)
        return token_resolver

    def locate_locking_realm(self):
        """
        the first realm that is identified as a LockingRealm will be used to
        lock all accounts
        """
        for realm in self.realms:
            if isinstance(realm, realm_abcs.LockingRealm):
                return realm
        return None

    def authenticate_single_realm_account(self, realm, authc_token):
        return realm.authenticate_account(authc_token)

    def authenticate_multi_realm_account(self, realms, authc_token):
        attempt = DefaultAuthenticationAttempt(authc_token, realms)
        return self.authentication_strategy.execute(attempt)

    def authenticate_account(self, identifiers, authc_token):
        """
        :type identifiers: SimpleIdentifierCollection or None

        :returns: account_id if the account authenticates
        :rtype: SimpleIdentifierCollection
        """
        msg = ("Authentication submission received for authentication "
               "token [" + str(authc_token) + "]")
        logger.debug(msg)

        # the following conditions verify correct authentication sequence
        if not getattr(authc_token, 'identifier', None):
            if not identifiers:
                msg = "Authentication must be performed in expected sequence."
                raise InvalidAuthenticationSequenceException(msg)
            authc_token.identifier = identifiers.primary_identifier

        try:
            account = self.do_authenticate_account(authc_token)
            if (account is None):
                msg2 = ("No account returned by any configured realms for "
                        "submitted authentication token [{0}]".
                        format(authc_token))

                raise UnknownAccountException(msg2)

        except AdditionalAuthenticationRequired as exc:
            self.notify_progress(authc_token.identifier)
            try:
                self.mfa_challenger.send_challenge(exc.account_id)
            except AttributeError:
                pass
            raise exc # the security_manager saves subject identifiers

        except IncorrectCredentialsException as exc:
            self.notify_failure(authc_token.identifier)
            self.validate_locked(authc_token, exc.account)
            raise  # this won't be called if the Account is locked

        self.notify_success(account['account_id'].primary_identifier)

        return account['account_id']

    def do_authenticate_account(self, authc_token):
        """
        Returns an account object only when the current token authenticates AND
        the authentication process is complete, raising otherwise

        :returns:  Account
        :raises AdditionalAuthenticationRequired: when additional tokens are required,
                                                  passing the account object
        """
        realms = self.token_realm_resolver[authc_token.__class__]

        # account is a dict:
        if (len(self.realms) == 1):
            account = self.authenticate_single_realm_account(realms[0], authc_token)
        else:
            account = self.authenticate_multi_realm_account(self.realms, authc_token)

        # the following condition verifies whether the account uses MFA:
        if len(account['authc_info']) > authc_token.TIER:
            # the token authenticated but additional authentication is required
            self.notify_progress(authc_token.identifier)
            raise AdditionalAuthenticationRequired(account['account_id'])

        return account
    # --------------------------------------------------------------------------
    # Event Communication
    # --------------------------------------------------------------------------

    def clear_cache(self, items=None):
        """
        expects event object to be in the format of a session-stop or
        session-expire event, whose results attribute is a
        namedtuple(identifiers, session_key)
        """
        try:
            for realm in self.realms:
                identifiers = items.identifiers
                identifier = identifiers.from_source(realm.name)
                if identifier:
                    realm.clear_cached_credentials(identifier)
        except AttributeError:
            msg = ('Could not clear authc_info from cache after event. '
                   'items: ' + str(items))
            logger.warn(msg)

    def register_cache_clear_listener(self):
        if self.event_bus:
            self.event_bus.register(self.clear_cache, 'SESSION.EXPIRE')
            self.event_bus.is_registered(self.clear_cache, 'SESSION.EXPIRE')
            self.event_bus.register(self.clear_cache, 'SESSION.STOP')
            self.event_bus.is_registered(self.clear_cache, 'SESSION.STOP')

    def notify_locked(self, identifier):
        try:
            self.event_bus.publish('AUTHENTICATION.ACCOUNT_LOCKED',
                                   identifier=identifier)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.ACCOUNT_LOCKED event"
            raise AuthenticationEventException(msg)

    def notify_progress(self, identifier):
        try:
            self.event_bus.publish('AUTHENTICATION.PROGRESS',
                                   identifier=identifier)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.PROGRESS event"
            raise AuthenticationEventException(msg)

    def notify_success(self, identifier):
        try:
            self.event_bus.publish('AUTHENTICATION.SUCCEEDED',
                                   identifier=identifier)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.SUCCEEDED event"
            raise AuthenticationEventException(msg)

    def notify_failure(self, identifier):
        try:
            self.event_bus.publish('AUTHENTICATION.FAILED',
                                   identifier=identifier)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.FAILED event"
            raise AuthenticationEventException(msg)

    def validate_locked(self, authc_token, account):
        token = authc_token.__class__.__name__
        failed_attempts = account['authc_info'][token]['failed_attempts']

        if self.locking_limit:
            if len(failed_attempts) > self.locking_limit:
                self.locking_realm.lock_account(account)
                msg = ('Authentication attempts breached threshold.  Account'
                       'is now locked: ', str(account))
                self.notify_locked(account['account_id'].primary_identifier)
                raise LockedAccountException(msg)

    def __repr__(self):
        return "<DefaultAuthenticator(event_bus={0}, strategy={0})>".\
            format(self.event_bus, self.authentication_strategy)
