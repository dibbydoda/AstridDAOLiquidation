import web3.eth
from web3 import Web3
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3.middleware import construct_sign_and_send_raw_middleware
from abi import EventManagerABI, SortedVaultsABI, PriceFeedABI, DaiOracleABI
from dotenv import load_dotenv
import os
import threading
import time
import asyncio
import json


load_dotenv()

KEY = os.getenv("KEY")
RPC_URL_WS = "ws://localhost:8546"
RPC_URL_HTTP = "http://localhost:9933"
VAULT_MANAGER_ADDRESS = "0x0cF3E16948418649498b59c336ba38678842E2d4"
SORTED_VAULTS_ADDRESS = "0x2Be04114c9F02981Ee92AcC0d4B4ec48A2CC68cF"
PRICE_FEED_ADDRESS = "0xb2c9eb6B5835d3DC1d9428673ECF957D8b008Bf9"
DIA_ORACLE_ADDRESS = "0xd79357ebb0cd724e391f2b49a8De0E31688fEc75"

sent_transactions = []


def initialise_connection(rpc):
    w3 = Web3(Web3.WebsocketProvider(rpc))
    account: LocalAccount = Account.from_key(KEY)
    w3.middleware_onion.add(construct_sign_and_send_raw_middleware(account))
    w3.eth.defaultAccount = account.address
    print(f"Connected to ChainID={w3.eth.chain_id} at BlockNumber={w3.eth.block_number}."
          f"\nTransactions will be sent from account {account.address}.")
    return w3, account


def initialise_manager_data(vault_manager):
    mcr = vault_manager.caller.MCR()
    return mcr


def check_collateral_price(feed_contract):
    price = feed_contract.caller.fetchPrice()
    return price


def vault_is_safe(vault_manager: web3.eth.Contract, vault_owner, mcr, price):
    vault_ICR = vault_manager.caller.getCurrentICR(vault_owner, price)
    print(f'ICR for Vault {vault_owner} is {vault_ICR/1000000000000000000:5.4f}')
    return vault_ICR > mcr


def vault_is_active(vault_manager: web3.eth.Contract, vault_owner):
    status = vault_manager.caller.getVaultStatus(vault_owner)
    return status == 1


def get_riskiest_vault_address(sorted_vaults: web3.eth.Contract):
    riskiest = sorted_vaults.caller.getLast()
    return riskiest


def get_next_vault(sorted_vaults: web3.eth.Contract, current_vault):
    next_vault = sorted_vaults.caller.getPrev(current_vault)
    return next_vault


def liquidate_vaults(vault_manager_contract, sorted_vault_owners_contract, connection_to_chain):
    stop = False

    def liquidate_vault(vault_manager: web3.eth.Contract, vault_owner, connection: Web3):
        global sent_transactions
        print(f"Attempting to liquidate {vault_owner}")
        try:
            vault_manager.functions.liquidate(vault_owner).call()
        except ValueError as e:
            print(e.args[0]['message'])
            if vault_is_active(vault_manager, vault_owner):
                nonlocal stop
                stop = True
            else:
                return
        else:
            transaction_hash = vault_manager.functions.liquidate(vault_owner).transact({
                        'maxFeePerGas': Web3.toWei(5, 'gwei'),
                        'maxPriorityFeePerGas': Web3.toWei(5, 'gwei'),
            })
            sent_transactions.append(transaction_hash)

    target_vault = get_riskiest_vault_address(sorted_vault_owners_contract)
    vault_threads = []
    while not stop:
        vault_thread = threading.Thread(target=liquidate_vault, args=(vault_manager_contract,
                                                                      target_vault,
                                                                      connection_to_chain))
        vault_threads.append(vault_thread)
        vault_thread.start()
        target_vault = get_next_vault(sorted_vaults=sorted_vault_owners_contract, current_vault=target_vault)
    print("Stopped Checking Vaults")
    for thread in vault_threads:
        thread.join()


def get_gas_price(connection: Web3):
    gas_price = connection.eth.gas_price
    return gas_price


def wait_for_receipt(txn_hash, connection):
    transaction_receipt = connection.eth.wait_for_transaction_receipt(transaction_hash=txn_hash)
    print(transaction_receipt)


async def get_event():
    global sent_transactions

    async with connect("wss://astar.api.onfinality.io/ws?apikey=e1452126-1bc9-409a-b663-a7ae8e150c8b") as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
        subscription_response = await ws.recv()
        print(subscription_response)
        # you are now subscribed to the event
        # you keep trying to listen to new events (similar idea to longPolling)
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=60)
                print(f"Block {int(json.loads(message)['params']['result']['number'], 16)} received")
                liquidate_vaults(vault_manager_contract=vault_manager,
                                 sorted_vault_owners_contract=sorted_vaults,
                                 connection_to_chain=w3connection)
                for transaction_hash in sent_transactions:
                    thread = threading.Thread(target=wait_for_receipt, args=(transaction_hash, w3connection),
                                              daemon=True)
                    thread.start()
                sent_transactions.clear()
            except Exception as e:
                print(e)
                break


def main():
    w3connection, account = initialise_connection(RPC_URL_WS)
    vault_manager = w3connection.eth.contract(address=VAULT_MANAGER_ADDRESS, abi=EventManagerABI)
    sorted_vaults = w3connection.eth.contract(address=SORTED_VAULTS_ADDRESS, abi=SortedVaultsABI)
    price_feed = w3connection.eth.contract(address=PRICE_FEED_ADDRESS, abi=PriceFeedABI)
    oracle_feed = w3connection.eth.contract(address=DIA_ORACLE_ADDRESS, abi=DaiOracleABI)
    block_filter = w3connection.eth.filter({'fromBlock': 'latest', 'toBlock': 'pending'})

    while True:
        for transaction in block_filter.get_new_entries():
            print(transaction)
        time.sleep(0.1)


if __name__ == "__main__":
    main()
















