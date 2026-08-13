from web3 import Web3

RPC_URL = "https://mainnet.base.org"
PAIR_ADDRESS = "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"


def get_web3():
    return Web3(Web3.HTTPProvider(RPC_URL))


def check_pair():
    w3 = get_web3()
    print(f"Checking pair {PAIR_ADDRESS} on {RPC_URL}")

    # Check code
    code = w3.eth.get_code(PAIR_ADDRESS)
    if len(code) <= 2:
        print("Contract does not exist (no code)!")
        return

    print("Contract code exists.")

    # Try getReserves (UniV2/Aerodrome V2)
    # Selector: 0x0902f1ac
    try:
        res = w3.eth.call({"to": PAIR_ADDRESS, "data": "0x0902f1ac"})
        print(f"getReserves success: {res.hex()}")
    except Exception as e:
        print(f"getReserves failed: {e}")

    # Try slot0 (UniV3/CL)
    # Selector: 0x3850c7bd
    try:
        res = w3.eth.call({"to": PAIR_ADDRESS, "data": "0x3850c7bd"})
        print(f"slot0 success: {res.hex()}")
    except Exception as e:
        print(f"slot0 failed: {e}")

    # Try globalState (Aerodrome CL maybe?)
    # try factory?


if __name__ == "__main__":
    check_pair()
