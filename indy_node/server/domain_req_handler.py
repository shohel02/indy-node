from copy import deepcopy
from typing import List

import base58

from indy_common.auth import Authoriser
from indy_common.constants import NYM, ROLE, ATTRIB, SCHEMA, CLAIM_DEF, REF, \
    GET_NYM, GET_ATTR, GET_SCHEMA, GET_CLAIM_DEF, SIGNATURE_TYPE, REVOC_REG_DEF, REVOC_REG_ENTRY, ISSUANCE_TYPE, \
    REVOC_REG_DEF_ID, VALUE, ISSUANCE_BY_DEFAULT, ISSUANCE_ON_DEMAND, TAG, CRED_DEF_ID, \
    GET_REVOC_REG_DEF, ID, GET_REVOC_REG, GET_REVOC_REG_DELTA, ATTR_NAMES, REVOC_TYPE, \
    TIMESTAMP, ACCUM, FROM, TO, ISSUED, REVOKED, STATE_PROOF_FROM, REVOC_REG_ID, ACCUM_FROM, ACCUM_TO
from indy_common.roles import Roles
from indy_common.state import domain
from indy_common.types import Request
from plenum.common.constants import TXN_TYPE, TARGET_NYM, RAW, ENC, HASH, \
    VERKEY, DATA, NAME, VERSION, ORIGIN, \
    TXN_TIME
from plenum.common.exceptions import InvalidClientRequest, \
    UnauthorizedClientRequest, UnknownIdentifier, InvalidClientMessageException
from plenum.common.types import f
from plenum.common.constants import TRUSTEE
from plenum.server.domain_req_handler import DomainRequestHandler as PHandler
from stp_core.common.log import getlogger
from indy_node.server.revocation_strategy import RevokedStrategy, IssuedStrategy

logger = getlogger()


class DomainReqHandler(PHandler):
    write_types = {NYM, ATTRIB, SCHEMA, CLAIM_DEF, REVOC_REG_DEF, REVOC_REG_ENTRY}
    query_types = {GET_NYM, GET_ATTR, GET_SCHEMA, GET_CLAIM_DEF,
                   GET_REVOC_REG_DEF, GET_REVOC_REG, GET_REVOC_REG_DELTA}
    revocation_strategy_map = {
        ISSUANCE_BY_DEFAULT: RevokedStrategy,
        ISSUANCE_ON_DEMAND: IssuedStrategy,
    }

    def __init__(self, ledger, state, config, requestProcessor,
                 idrCache, attributeStore, bls_store, tsRevoc_store):
        super().__init__(ledger, state, config, requestProcessor, bls_store)
        self.idrCache = idrCache
        self.attributeStore = attributeStore
        self.query_handlers = {
            GET_NYM: self.handleGetNymReq,
            GET_ATTR: self.handleGetAttrsReq,
            GET_SCHEMA: self.handleGetSchemaReq,
            GET_CLAIM_DEF: self.handleGetClaimDefReq,
            GET_REVOC_REG_DEF: self.handleGetRevocRegDefReq,
            GET_REVOC_REG: self.handleGetRevocRegReq,
            GET_REVOC_REG_DELTA: self.handleGetRevocRegDelta,
        }
        self.tsRevoc_store = tsRevoc_store

    def onBatchCreated(self, stateRoot):
        self.idrCache.currentBatchCreated(stateRoot)

    def onBatchRejected(self):
        self.idrCache.batchRejected()

    def get_revocation_strategy(self, typ):
        return self.revocation_strategy_map.get(typ, None)

    def _updateStateWithSingleTxn(self, txn, isCommitted=False):
        typ = txn.get(TXN_TYPE)
        nym = txn.get(TARGET_NYM)
        if typ == NYM:
            data = {
                f.IDENTIFIER.nm: txn.get(f.IDENTIFIER.nm),
                f.SEQ_NO.nm: txn.get(f.SEQ_NO.nm),
                TXN_TIME: txn.get(TXN_TIME)
            }
            if ROLE in txn:
                data[ROLE] = txn.get(ROLE)
            if VERKEY in txn:
                data[VERKEY] = txn.get(VERKEY)
            self.updateNym(nym, data, isCommitted=isCommitted)
        elif typ == ATTRIB:
            self._addAttr(txn)
        elif typ == SCHEMA:
            self._addSchema(txn)
        elif typ == CLAIM_DEF:
            self._addClaimDef(txn)
        elif typ == REVOC_REG_DEF:
            self._addRevocDef(txn)
        elif typ == REVOC_REG_ENTRY:
            self._addRevocRegEntry(txn)
        else:
            logger.debug(
                'Cannot apply request of type {} to state'.format(typ))

    def commit(self, txnCount, stateRoot, txnRoot, ppTime) -> List:
        r = super().commit(txnCount, stateRoot, txnRoot, ppTime)
        stateRoot = base58.b58decode(stateRoot.encode())
        self.idrCache.onBatchCommitted(stateRoot)
        self.tsRevoc_store.set(ppTime, stateRoot)
        return r

    def doStaticValidation(self, request: Request):
        identifier, req_id, operation = request.identifier, request.reqId, request.operation
        if operation[TXN_TYPE] == NYM:
            self._doStaticValidationNym(identifier, req_id, operation)
        if operation[TXN_TYPE] == ATTRIB:
            self._doStaticValidationAttrib(identifier, req_id, operation)
        if operation[TXN_TYPE] == SCHEMA:
            self._doStaticValidationSchema(identifier, req_id, operation)
        if operation[TXN_TYPE] == GET_REVOC_REG_DELTA:
            self._doStaticValidationGetRevocRegDelta(request)

    def _doStaticValidationNym(self, identifier, reqId, operation):
        role = operation.get(ROLE)
        nym = operation.get(TARGET_NYM)
        if not nym:
            raise InvalidClientRequest(identifier, reqId,
                                       "{} needs to be present".
                                       format(TARGET_NYM))
        if not Authoriser.isValidRole(role):
            raise InvalidClientRequest(identifier, reqId,
                                       "{} not a valid role".
                                       format(role))

    def _doStaticValidationAttrib(self, identifier, reqId, operation):
        if not self._validate_attrib_keys(operation):
            raise InvalidClientRequest(identifier, reqId,
                                       '{} should have one and only one of '
                                       '{}, {}, {}'
                                       .format(ATTRIB, RAW, ENC, HASH))

    def _doStaticValidationSchema(self, identifier, reqId, operation):
        if not operation.get(DATA).get(ATTR_NAMES):
            raise InvalidClientRequest(identifier, reqId,
                                       "attr_names in schema can not be empty")

    def validate(self, req: Request, config=None):
        op = req.operation
        typ = op[TXN_TYPE]

        if typ == NYM:
            self._validateNym(req)
        elif typ == ATTRIB:
            self._validateAttrib(req)
        elif typ == SCHEMA:
            self._validate_schema(req)
        elif typ == CLAIM_DEF:
            self._validate_claim_def(req)
        elif typ == REVOC_REG_DEF:
            self._validate_revoc_reg_def(req)
        elif typ == REVOC_REG_ENTRY:
            self._validate_revoc_reg_entry(req)

    @staticmethod
    def _validate_attrib_keys(operation):
        dataKeys = {RAW, ENC, HASH}.intersection(set(operation.keys()))
        return len(dataKeys) == 1

    def _validateNym(self, req: Request):
        origin = req.identifier
        op = req.operation

        try:
            originRole = self.idrCache.getRole(
                origin, isCommitted=False) or None
        except BaseException:
            raise UnknownIdentifier(
                req.identifier,
                req.reqId)

        nymData = self.idrCache.getNym(op[TARGET_NYM], isCommitted=False)
        if not nymData:
            # If nym does not exist
            self._validateNewNym(req, op, originRole)
        else:
            self._validateExistingNym(req, op, originRole, nymData)

    def _validateNewNym(self, req: Request, op, originRole):
        role = op.get(ROLE)
        r, msg = Authoriser.authorised(NYM,
                                       originRole,
                                       field=ROLE,
                                       oldVal=None,
                                       newVal=role)
        if not r:
            raise UnauthorizedClientRequest(
                req.identifier,
                req.reqId,
                "{} cannot add {}".format(
                    Roles.nameFromValue(originRole),
                    Roles.nameFromValue(role))
            )

    def _validateExistingNym(self, req: Request, op, originRole, nymData):
        unauthorized = False
        reason = None
        origin = req.identifier
        owner = self.idrCache.getOwnerFor(op[TARGET_NYM], isCommitted=False)
        isOwner = origin == owner

        if not originRole == TRUSTEE and not isOwner:
            reason = '{} is neither Trustee nor owner of {}' \
                .format(origin, op[TARGET_NYM])
            unauthorized = True

        if not unauthorized:
            updateKeys = [ROLE, VERKEY]
            for key in updateKeys:
                if key in op:
                    newVal = op[key]
                    oldVal = nymData.get(key)
                    if oldVal != newVal:
                        r, msg = Authoriser.authorised(NYM, originRole, field=key,
                                                       oldVal=oldVal, newVal=newVal,
                                                       isActorOwnerOfSubject=isOwner)
                        if not r:
                            unauthorized = True
                            reason = "{} cannot update {}".\
                                format(Roles.nameFromValue(originRole), key)
                            break
        if unauthorized:
            raise UnauthorizedClientRequest(
                req.identifier, req.reqId, reason)

    def _validateAttrib(self, req: Request):
        origin = req.identifier
        op = req.operation

        if not (not op.get(TARGET_NYM) or
                self.hasNym(op[TARGET_NYM], isCommitted=False)):
            raise InvalidClientRequest(origin, req.reqId,
                                       '{} should be added before adding '
                                       'attribute for it'.
                                       format(TARGET_NYM))

        if op.get(TARGET_NYM) and op[TARGET_NYM] != req.identifier and \
                not self.idrCache.getOwnerFor(op[TARGET_NYM],
                                              isCommitted=False) == origin:
            raise UnauthorizedClientRequest(
                req.identifier,
                req.reqId,
                "Only identity owner/guardian can add attribute "
                "for that identity")

    def _validate_schema(self, req: Request):
        # we can not add a Schema with already existent NAME and VERSION
        # sine a Schema needs to be identified by seqNo
        identifier = req.identifier
        operation = req.operation
        schema_name = operation[DATA][NAME]
        schema_version = operation[DATA][VERSION]
        schema, _, _, _ = self.getSchema(
            author=identifier,
            schemaName=schema_name,
            schemaVersion=schema_version
        )
        if schema:
            raise InvalidClientRequest(identifier, req.reqId,
                                       '{} can have one and only one SCHEMA with '
                                       'name {} and version {}'
                                       .format(identifier, schema_name, schema_version))
        try:
            origin_role = self.idrCache.getRole(
                req.identifier, isCommitted=False) or None
        except BaseException:
            raise UnknownIdentifier(
                req.identifier,
                req.reqId)
        r, msg = Authoriser.authorised(typ=SCHEMA,
                                       actorRole=origin_role)
        if not r:
            raise UnauthorizedClientRequest(
                req.identifier,
                req.reqId,
                "{} cannot add schema".format(
                    Roles.nameFromValue(origin_role))
            )

    def _validate_claim_def(self, req: Request):
        # we can not add a Claim Def with existent ISSUER_DID
        # sine a Claim Def needs to be identified by seqNo
        try:
            origin_role = self.idrCache.getRole(
                req.identifier, isCommitted=False) or None
        except BaseException:
            raise UnknownIdentifier(
                req.identifier,
                req.reqId)
        # only owner can update claim_def,
        # because his identifier is the primary key of claim_def
        r, msg = Authoriser.authorised(typ=CLAIM_DEF,
                                       actorRole=origin_role,
                                       isActorOwnerOfSubject=True)
        if not r:
            raise UnauthorizedClientRequest(
                req.identifier,
                req.reqId,
                "{} cannot add claim def".format(
                    Roles.nameFromValue(origin_role))
            )

    def _validate_revoc_reg_def(self, req: Request):
        operation = req.operation
        cred_def_id = operation.get(CRED_DEF_ID)
        revoc_def_type = operation.get(REVOC_TYPE)
        revoc_def_tag = operation.get(TAG)
        assert cred_def_id
        assert revoc_def_tag
        assert revoc_def_type
        cred_def, _, _, _ = self.lookup(cred_def_id, isCommitted=False)
        if cred_def is None:
            raise InvalidClientRequest(req.identifier,
                                       req.reqId,
                                       "There is no any CRED_DEF by path: {}".format(cred_def_id))

    def _get_current_revoc_entry_and_revoc_def(self, author_did, revoc_reg_def_id, req_id):
        assert author_did
        assert revoc_reg_def_id
        current_entry, _, _, _ = self.getRevocDefEntry(author_did=author_did,
                                                       revoc_reg_def_id=revoc_reg_def_id,
                                                       isCommitted=False)
        revoc_def, _, _, _ = self.lookup(revoc_reg_def_id, isCommitted=False)
        if revoc_def is None:
            raise InvalidClientRequest(author_did,
                                       req_id,
                                       "There is no any REVOC_REG_DEF by path: {}".format(revoc_reg_def_id))
        return current_entry, revoc_def

    def _validate_revoc_reg_entry(self, req: Request):
        current_entry, revoc_def = self._get_current_revoc_entry_and_revoc_def(
            author_did=req.identifier,
            revoc_reg_def_id=req.operation[REVOC_REG_DEF_ID],
            req_id=req.reqId
        )
        validator_cls = self.get_revocation_strategy(revoc_def[VALUE][ISSUANCE_TYPE])
        validator = validator_cls(self.state)
        validator.validate(current_entry, req)

    def updateNym(self, nym, data, isCommitted=True):
        updatedData = super().updateNym(nym, data, isCommitted=isCommitted)
        txn_time = data.get(TXN_TIME)
        self.idrCache.set(nym,
                          seqNo=data[f.SEQ_NO.nm],
                          txnTime=txn_time,
                          ta=updatedData.get(f.IDENTIFIER.nm),
                          role=updatedData.get(ROLE),
                          verkey=updatedData.get(VERKEY),
                          isCommitted=isCommitted)

    def hasNym(self, nym, isCommitted: bool = True):
        return self.idrCache.hasNym(nym, isCommitted=isCommitted)

    def handleGetNymReq(self, request: Request):
        nym = request.operation[TARGET_NYM]
        nymData = self.idrCache.getNym(nym, isCommitted=True)
        path = domain.make_state_path_for_nym(nym)
        if nymData:
            nymData[TARGET_NYM] = nym
            data = self.stateSerializer.serialize(nymData)
            seq_no = nymData[f.SEQ_NO.nm]
            update_time = nymData[TXN_TIME]
            proof = self.make_proof(path)
        else:
            data = None
            seq_no = None
            proof = self.make_proof(path)
            update_time = None

        # TODO: add update time here!
        result = self.make_result(request=request,
                                  data=data,
                                  last_seq_no=seq_no,
                                  update_time=update_time,
                                  proof=proof)

        result.update(request.operation)
        return result

    def handleGetSchemaReq(self, request: Request):
        author_did = request.operation[TARGET_NYM]
        schema_name = request.operation[DATA][NAME]
        schema_version = request.operation[DATA][VERSION]
        schema, lastSeqNo, lastUpdateTime, proof = self.getSchema(
            author=author_did,
            schemaName=schema_name,
            schemaVersion=schema_version
        )
        # TODO: we have to do this since SCHEMA has a bit different format than other txns
        # (it has NAME and VERSION inside DATA, and it's not part of the state value, but state key)
        if schema is None:
            schema = {}
        schema.update({
            NAME: schema_name,
            VERSION: schema_version
        })
        return self.make_result(request=request,
                                data=schema,
                                last_seq_no=lastSeqNo,
                                update_time=lastUpdateTime,
                                proof=proof)

    def handleGetClaimDefReq(self, request: Request):
        signatureType = request.operation[SIGNATURE_TYPE]
        keys, lastSeqNo, lastUpdateTime, proof = self.getClaimDef(
            author=request.operation[ORIGIN],
            schemaSeqNo=request.operation[REF],
            signatureType=signatureType
        )
        result = self.make_result(request=request,
                                  data=keys,
                                  last_seq_no=lastSeqNo,
                                  update_time=lastUpdateTime,
                                  proof=proof)
        result[SIGNATURE_TYPE] = signatureType
        return result

    def handleGetRevocRegDefReq(self, request: Request):
        state_path = request.operation.get(ID, None)
        assert state_path
        try:
            keys, last_seq_no, last_update_time, proof = self.lookup(state_path, isCommitted=True)
        except KeyError:
            keys, last_seq_no, last_update_time, proof = None, None, None, None
        result = self.make_result(request=request,
                                  data=keys,
                                  last_seq_no=last_seq_no,
                                  update_time=last_update_time,
                                  proof=proof)
        return result

    def handleGetRevocRegReq(self, request: Request):
        req_ts = request.operation.get(TIMESTAMP)
        revoc_reg_def_id = request.operation.get(REVOC_REG_DEF_ID)
        author_did = request.identifier
        # Get root hash corresponding with given timestamp
        past_root = self.tsRevoc_store.get_equal_or_prev(req_ts)
        # Path to corresponding ACCUM record in state
        path = domain.make_state_path_for_revoc_reg_entry_accum(authors_did=author_did,
                                                                revoc_reg_def_id=revoc_reg_def_id)
        if past_root is None:
            reply_data = None
            seq_no = None
            last_update_time = None
            proof = None
        else:
            encoded_entry = self.state.get_for_root_hash(past_root, path)
            revoc_reg_entry_accum, seq_no, last_update_time = domain.decode_state_value(encoded_entry)

            reply_data = None if revoc_reg_entry_accum is None else revoc_reg_entry_accum

            proof = self.make_proof(path, head_hash=past_root)
        return self.make_result(request=request,
                                data=reply_data,
                                last_seq_no=seq_no,
                                update_time=last_update_time,
                                proof=proof)

    def _get_reg_entry_by_timestamp(self, timestamp, path_to_reg_entry):
        reg_entry = None
        past_root = self.tsRevoc_store.get_equal_or_prev(timestamp)
        if past_root:
            encoded_entry = self.state.get_for_root_hash(past_root, path_to_reg_entry)
            assert encoded_entry
            reg_entry, _, _ = domain.decode_state_value(encoded_entry)
        return past_root, reg_entry

    def _get_reg_entry_accum_by_timestamp(self, timestamp, path_to_reg_entry_accum):
        reg_entry_accum = None
        seq_no = None
        last_update_time = None
        reg_entry_accum_proof = None
        past_root = self.tsRevoc_store.get_equal_or_prev(timestamp)
        if past_root:
            encoded_entry = self.state.get_for_root_hash(past_root, path_to_reg_entry_accum)
            reg_entry_accum, seq_no, last_update_time = domain.decode_state_value(encoded_entry)
            reg_entry_accum_proof = self.make_proof(path_to_reg_entry_accum, head_hash=past_root)
        return reg_entry_accum, seq_no, last_update_time, reg_entry_accum_proof

    def _doStaticValidationGetRevocRegDelta(self, request: Request):
        assert request.operation
        req_ts_to = request.operation.get(TO, None)
        assert req_ts_to
        req_ts_from = request.operation.get(FROM, None)
        if req_ts_from and req_ts_from > req_ts_to:
            raise InvalidClientRequest(request.identifier,
                                       request.reqId,
                                       "Timestamp FROM more then TO: {} > {}".format(req_ts_from, req_ts_to))

    def handleGetRevocRegDelta(self, request: Request):
        """
        For getting reply we need:
        1. Get REVOC_REG_ENTRY by "TO" timestamp from state
        2. If FROM is given in request, then Get REVOC_REG_ENTRY by "FROM" timestamp from state
        3. Get ISSUANCE_TYPE for REVOC_REG_DEF (revoked/issued strategy)
        4. Compute issued and revoked indices by corresponding strategy
        5. Make result
           5.1 Now, if "FROM" is presented in request, then STATE_PROOF_FROM and ACCUM (revocation entry for "FROM" timestamp)
               will added into data section
           5.2 If not, then only STATE_PROOF for "TO" revocation entry will added
        :param request:
        :return: Reply
        """
        req_ts_from = request.operation.get(FROM, None)
        req_ts_to = request.operation.get(TO)
        revoc_reg_def_id = request.operation.get(REVOC_REG_DEF_ID)
        author_did = request.identifier
        reply = None
        # Get root hash for "to" timestamp
        # Get REVOC_REG_ENTRY and ACCUM record for timestamp "to"
        path_to_reg_entry = domain.make_state_path_for_revoc_reg_entry(authors_did=author_did,
                                                                       revoc_reg_def_id=revoc_reg_def_id)
        path_to_reg_entry_accum = domain.make_state_path_for_revoc_reg_entry_accum(authors_did=author_did,
                                                                                   revoc_reg_def_id=revoc_reg_def_id)
        past_root_to, reg_entry_to = self._get_reg_entry_by_timestamp(req_ts_to, path_to_reg_entry)
        reg_entry_accum_to, \
            seq_no_to, \
            last_update_time_to, \
            reg_entry_accum_proof_to = self._get_reg_entry_accum_by_timestamp(req_ts_to, path_to_reg_entry_accum)
        if past_root_to:
            # Get issuance type from REVOC_REG_DEF
            encoded_revoc_reg_def = self.state.get_for_root_hash(past_root_to, revoc_reg_def_id)
            assert encoded_revoc_reg_def
            revoc_reg_def, _, _ = domain.decode_state_value(encoded_revoc_reg_def)
            strategy_cls = self.get_revocation_strategy(revoc_reg_def[VALUE][ISSUANCE_TYPE])

            if req_ts_from:
                past_root_from, reg_entry_from = self._get_reg_entry_by_timestamp(req_ts_from, path_to_reg_entry)
                req_entry_accum_from, \
                    seq_no_from, \
                    last_update_time_from, \
                    reg_entry_accum_proof_from = self._get_reg_entry_accum_by_timestamp(req_ts_from, path_to_reg_entry_accum)
                # Compute issued/revoked lists corresponding with ISSUANCE_TYPE strategy
                result_issued, result_revoked = strategy_cls.get_delta({ISSUED: reg_entry_to[VALUE][ISSUED],
                                                                        REVOKED: reg_entry_to[VALUE][REVOKED]},
                                                                       {ISSUED: reg_entry_from[VALUE][ISSUED],
                                                                        REVOKED: reg_entry_from[VALUE][REVOKED]})
            else:
                result_issued, result_revoked = strategy_cls.get_delta({ISSUED: reg_entry_to[VALUE][ISSUED],
                                                                        REVOKED: reg_entry_to[VALUE][REVOKED]},
                                                                       None)
            reply = {
                REVOC_REG_ID: str(path_to_reg_entry),
                REVOC_TYPE: revoc_reg_def.get(REVOC_TYPE),
                VALUE: {
                    ACCUM_TO: reg_entry_accum_to,
                    ISSUED: result_issued,
                    REVOKED: result_revoked
                }

            }
            # If we got "from" timestamp, then add state proof into "data" section of reply
            if req_ts_from:
                reply[STATE_PROOF_FROM] = reg_entry_accum_proof_from
                reply[VALUE][ACCUM_FROM] = req_entry_accum_from

        return self.make_result(request=request,
                                data=reply,
                                last_seq_no=seq_no_to,
                                update_time=last_update_time_to,
                                proof=reg_entry_accum_proof_to)

    def handleGetAttrsReq(self, request: Request):
        if not self._validate_attrib_keys(request.operation):
            raise InvalidClientRequest(request.identifier, request.reqId,
                                       '{} should have one and only one of '
                                       '{}, {}, {}'
                                       .format(ATTRIB, RAW, ENC, HASH))
        nym = request.operation[TARGET_NYM]
        if RAW in request.operation:
            attr_type = RAW
        elif ENC in request.operation:
            # If attribute is encrypted, it will be queried by its hash
            attr_type = ENC
        else:
            attr_type = HASH
        attr_key = request.operation[attr_type]
        value, lastSeqNo, lastUpdateTime, proof = \
            self.getAttr(did=nym, key=attr_key, attr_type=attr_type)
        attr = None
        if value is not None:
            if HASH in request.operation:
                attr = attr_key
            else:
                attr = value
        return self.make_result(request=request,
                                data=attr,
                                last_seq_no=lastSeqNo,
                                update_time=lastUpdateTime,
                                proof=proof)

    def lookup(self, path, isCommitted=True) -> (str, int):
        """
        Queries state for data on specified path

        :param path: path to data
        :return: data
        """
        assert path is not None
        encoded = self.state.get(path, isCommitted)
        proof = self.make_proof(path)
        if encoded is not None:
            value, last_seq_no, last_update_time = domain.decode_state_value(encoded)
            return value, last_seq_no, last_update_time, proof
        return None, None, None, proof

    def _addAttr(self, txn) -> None:
        """
        The state trie stores the hash of the whole attribute data at:
            the did+attribute name if the data is plaintext (RAW)
            the did+hash(attribute) if the data is encrypted (ENC)
        If the attribute is HASH, then nothing is stored in attribute store,
        the trie stores a blank value for the key did+hash
        """
        assert txn[TXN_TYPE] == ATTRIB
        attr_type, path, value, hashed_value, value_bytes = domain.prepare_attr_for_state(txn)
        self.state.set(path, value_bytes)
        if attr_type != HASH:
            self.attributeStore.set(hashed_value, value)

    def _addSchema(self, txn) -> None:
        assert txn[TXN_TYPE] == SCHEMA
        path, value_bytes = domain.prepare_schema_for_state(txn)
        self.state.set(path, value_bytes)

    def _addClaimDef(self, txn) -> None:
        assert txn[TXN_TYPE] == CLAIM_DEF
        path, value_bytes = domain.prepare_claim_def_for_state(txn)
        self.state.set(path, value_bytes)

    def _addRevocDef(self, txn) -> None:
        assert txn[TXN_TYPE] == REVOC_REG_DEF
        path, value_bytes = domain.prepare_revoc_def_for_state(txn)
        self.state.set(path, value_bytes)

    def _addRevocRegEntry(self, txn) -> None:
        current_entry, revoc_def = self._get_current_revoc_entry_and_revoc_def(
            author_did=txn[f.IDENTIFIER.nm],
            revoc_reg_def_id=txn[REVOC_REG_DEF_ID],
            req_id=txn[f.REQ_ID.nm]
        )
        writer_cls = self.get_revocation_strategy(revoc_def[VALUE][ISSUANCE_TYPE])
        writer = writer_cls(self.state)
        writer.write(current_entry, txn)

    def getAttr(self,
                did: str,
                key: str,
                attr_type,
                isCommitted=True) -> (str, int, int, list):
        assert did is not None
        assert key is not None
        path = domain.make_state_path_for_attr(did, key, attr_type == HASH)
        try:
            hashed_val, lastSeqNo, lastUpdateTime, proof = \
                self.lookup(path, isCommitted)
        except KeyError:
            return None, None, None, None
        if not hashed_val or hashed_val == '':
            # Its a HASH attribute
            return hashed_val, lastSeqNo, lastUpdateTime, proof
        else:
            try:
                value = self.attributeStore.get(hashed_val)
            except KeyError:
                logger.error('Could not get value from attribute store for {}'
                             .format(hashed_val))
                return None, None, None, None
        return value, lastSeqNo, lastUpdateTime, proof

    def getSchema(self,
                  author: str,
                  schemaName: str,
                  schemaVersion: str,
                  isCommitted=True) -> (str, int, int, list):
        assert author is not None
        assert schemaName is not None
        assert schemaVersion is not None
        path = domain.make_state_path_for_schema(author, schemaName, schemaVersion)
        try:
            keys, seqno, lastUpdateTime, proof = self.lookup(path, isCommitted)
            return keys, seqno, lastUpdateTime, proof
        except KeyError:
            return None, None, None, None

    def getClaimDef(self,
                    author: str,
                    schemaSeqNo: str,
                    signatureType='CL',
                    isCommitted=True) -> (str, int, int, list):
        assert author is not None
        assert schemaSeqNo is not None
        path = domain.make_state_path_for_claim_def(author, schemaSeqNo, signatureType)
        try:
            keys, seqno, lastUpdateTime, proof = self.lookup(path, isCommitted)
            return keys, seqno, lastUpdateTime, proof
        except KeyError:
            return None, None, None, None

    def getRevocDef(self,
                    author_did,
                    cred_def_id,
                    revoc_def_type,
                    revoc_def_tag,
                    isCommitted=True) -> (str, int, int, list):
        assert author_did is not None
        assert cred_def_id is not None
        assert revoc_def_type is not None
        assert revoc_def_tag is not None
        path = domain.make_state_path_for_revoc_def(author_did,
                                                    cred_def_id,
                                                    revoc_def_type,
                                                    revoc_def_tag)
        try:
            keys, seqno, lastUpdateTime, proof = self.lookup(path, isCommitted)
            return keys, seqno, lastUpdateTime, proof
        except KeyError:
            return None, None, None, None

    def getRevocDefEntry(self,
                         author_did,
                         revoc_reg_def_id,
                         isCommitted=True) -> (str, int, int, list):
        assert author_did
        assert revoc_reg_def_id
        path = domain.make_state_path_for_revoc_reg_entry(authors_did=author_did,
                                                          revoc_reg_def_id=revoc_reg_def_id)
        try:
            keys, seqno, lastUpdateTime, proof = self.lookup(path, isCommitted)
            return keys, seqno, lastUpdateTime, proof
        except KeyError:
            return None, None, None, None

    def get_query_response(self, request: Request):
        return self.query_handlers[request.operation[TXN_TYPE]](request)

    @staticmethod
    def transform_txn_for_ledger(txn):
        """
        Some transactions need to be transformed before they can be stored in the
        ledger, eg. storing certain payload in another data store and only its
        hash in the ledger
        """
        if txn[TXN_TYPE] == ATTRIB:
            txn = DomainReqHandler.transform_attrib_for_ledger(txn)
        return txn

    @staticmethod
    def transform_attrib_for_ledger(txn):
        """
        Creating copy of result so that `RAW`, `ENC` or `HASH` can be
        replaced by their hashes. We do not insert actual attribute data
        in the ledger but only the hash of it.
        """
        txn = deepcopy(txn)
        attr_type, _, value = domain.parse_attr_txn(txn)
        if attr_type in [RAW, ENC]:
            txn[attr_type] = domain.hash_of(value) if value else ''

        return txn
