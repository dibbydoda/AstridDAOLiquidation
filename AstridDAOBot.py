import web3.eth
from web3 import Web3
import web3.exceptions
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_account._utils.legacy_transactions import encode_transaction, serializable_unsigned_transaction_from_dict
from eth_utils.address import to_checksum_address
from eth_utils.curried import keccak
from web3.middleware import construct_sign_and_send_raw_middleware
from abi import EventManagerABI, SortedVaultsABI, PriceFeedABI, DaiOracleABI
import concurrent.futures
from dotenv import load_dotenv
import os
import threading
import time
import asyncio
import json
from substrateinterface import SubstrateInterface
import logging
from systemdlogging.toolbox import init_systemd_logging

load_dotenv()

KEY = os.getenv("KEY")
RPC_URL_WS = "wss://astar.api.onfinality.io/ws?apikey=e1452126-1bc9-409a-b663-a7ae8e150c8b"
VAULT_MANAGER_ADDRESS = "0x0cF3E16948418649498b59c336ba38678842E2d4"
SORTED_VAULTS_ADDRESS = "0x2Be04114c9F02981Ee92AcC0d4B4ec48A2CC68cF"
PRICE_FEED_ADDRESS = "0xb2c9eb6B5835d3DC1d9428673ECF957D8b008Bf9"
DIA_ORACLE_ADDRESS = "0xD7B7dc549A4C3E1113c9Ab92A82A31368082BCAc"
ORACLE_UPDATER_ADDRESS = '0xB81B557863BA92DdcA68DfE3171C646B0C132de1'


init_systemd_logging()
log = logging.getLogger('AstridDaoBot')
log.setLevel(logging.INFO)
log.info("Logging Started")


def initialise_connection(rpc):
    w3 = Web3(Web3.WebsocketProvider(rpc))
    account: LocalAccount = Account.from_key(KEY)
    w3.middleware_onion.add(construct_sign_and_send_raw_middleware(account))
    w3.eth.defaultAccount = account.address
    log.info(f"Connected to ChainID={w3.eth.chain_id} at BlockNumber={w3.eth.block_number}."
          f"\nTransactions will be sent from account {account.address}.")
    return w3, account


def get_next_vault(sorted_vaults, current_vault):
    next_vault = sorted_vaults.caller.getPrev(current_vault)
    return next_vault


def get_vaults_at_risk(vault_manager, sorted_vaults, new_price):
    mcr = vault_manager.caller.MCR()
    vaults_at_risk = 0
    riskiest_vault = sorted_vaults.caller.getLast()
    while True:
        log.info("Getting Vault")
        vault_icr = vault_manager.caller.getCurrentICR(riskiest_vault, new_price)
        log.info(f"{vault_icr}, {mcr}")
        if vault_icr < mcr:
            riskiest_vault = get_next_vault(sorted_vaults, riskiest_vault)
            vaults_at_risk += 1
            continue
        else:
            return vaults_at_risk


def liquidate_vaults(vault_manager: web3.eth.Contract, number, gas_price):
    transaction_hash = vault_manager.functions.liquidateVaults(number).transact({
                'maxFeePerGas': gas_price,
                'maxPriorityFeePerGas': gas_price,
                'gas': 10000000})


def is_set_astr_value(oracle_feed: web3.eth.Contract, transaction):
    input_function, input_parameters = oracle_feed.decode_function_input(transaction['data'])
    if "setValue" in str(input_function) and input_parameters['key'] == 'ASTR/USD':
        return True, input_parameters
    return False, None


def convert_extrinsic(decoded_extrinsic, desired_receiver):
    trans_type, args = list(decoded_extrinsic['call']['call_args'][0]['value'].items())[0]
    if args['action'] == 'Create':
        return
    to = args['action']['Call']
    if to != desired_receiver.lower():
        return
    data = args['input']
    nonce = args['nonce'][0]
    value = args['value'][0]
    v, r, s = args['signature'].values()
    if trans_type == "Legacy":
        gas_limit = args['gas_limit'][0]
        gas_price = args['gas_price'][0]
        transaction_dict = {
            'to': to_checksum_address(to),
            'value': value,
            'gas': gas_limit,
            'gasPrice': gas_price,
            'nonce': nonce,
            'data': data}
        unsigned_transaction = serializable_unsigned_transaction_from_dict(transaction_dict)
        encoded = encode_transaction(unsigned_transaction, (v, int(r, 16), int(s, 16)))
        transaction_dict['from'] = Account.recover_transaction(encoded)
        transaction_dict['hash'] = keccak(encoded)
        return transaction_dict
    else:
        log.warning("Found NON Legacy Transactions.")
        log.warning(f"{trans_type}")
        log.warning(f"{args}")


def extrinsic_is_eth_transact(decoded_extrinsic):
    call = decoded_extrinsic['call']
    return call["call_module"] == 'Ethereum' and call["call_function"] == "transact"


def wait_for_receipt(txn_hash, connection):
    receipt = connection.eth.wait_for_transaction_receipt(txn_hash)
    return receipt


def check_for_price_update(substrate, extrinsic_string, w3contracts):
    oracle = w3contracts["oracle_feed"]
    extrinsic = substrate.decode_scale(type_string="Extrinsic", scale_bytes=extrinsic_string)
    if extrinsic_is_eth_transact(extrinsic):
        converted = convert_extrinsic(extrinsic, DIA_ORACLE_ADDRESS)
        if converted is not None:
            is_setting_value, parameters = is_set_astr_value(oracle, converted)
            if is_setting_value and converted['from'] == ORACLE_UPDATER_ADDRESS:
                new_price = parameters["value"] * 10000000000
                return converted, new_price


def execute_order_66(w3connection, substrate, w3contracts):
    pending_transactions = substrate.rpc_request("author_pendingExtrinsics", []).get("result")
    log.debug(f"Received {len(pending_transactions)} transactions.")
    for extrinsic in pending_transactions:
        result = check_for_price_update(substrate, extrinsic, w3contracts)
        if result is not None:
            transaction, new_price = result
            vaults_at_risk = get_vaults_at_risk(w3contracts["vault_manager"], w3contracts["sorted_vaults"], new_price)
            log.info(f"On a price update to {new_price}, I found {vaults_at_risk} vaults at risk.")
            if vaults_at_risk > 0:
                executor = concurrent.futures.ThreadPoolExecutor()
                for i in range(10):
                    liquidate_vaults(w3contracts["vault_manager"], vaults_at_risk, transaction['gasPrice'])
                    try:
                        w3connection.eth.get_transaction(transaction['hash'])
                    except web3.exceptions.TransactionNotFound:
                        time.sleep(1)
                    else:
                        break



def nonce_farm(transactions, connection: Web3):
    for i in range(transactions):
        connection.eth.send_transaction({
            'to': '0x969656b9d58814824a8E329A96672243C7Ce8e76',
            'maxFeePerGas': Web3.toWei(1, "gwei"),
            'maxPriorityFeePerGas':  Web3.toWei(1, "gwei"),
            'value': 1})


if __name__ == "__main__":
    try:
        w3connection_obj, account = initialise_connection(RPC_URL_WS)
        substrate_obj = SubstrateInterface(url=RPC_URL_WS)
        substrate_obj.init_runtime()

        contracts = {
            "vault_manager": w3connection_obj.eth.contract(address=VAULT_MANAGER_ADDRESS, abi=EventManagerABI),
            "sorted_vaults": w3connection_obj.eth.contract(address=SORTED_VAULTS_ADDRESS, abi=SortedVaultsABI),
            "price_feed": w3connection_obj.eth.contract(address=PRICE_FEED_ADDRESS, abi=PriceFeedABI),
            "oracle_feed": w3connection_obj.eth.contract(address=DIA_ORACLE_ADDRESS, abi=DaiOracleABI)}

        while True:
            execute_order_66(w3connection_obj, substrate_obj, contracts)

    except Exception as e:
        log.exception(e)
        raise
















