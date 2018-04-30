#!/usr/bin/env python2
# Copyright (c) 2014-2015 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

#
# Test proper accounting with an equivalent malleability clone
#

from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import *

class TxnMallTest(BitcoinTestFramework):

    def add_options(self, parser):
        parser.add_option("--mineblock", dest="mine_block", default=False, action="store_true",
                          help="Test double-spend of 1-confirmed transaction")

    def setup_network(self):
        # Start with split network:
        return super(TxnMallTest, self).setup_network(True)

    def run_test(self):
        # All nodes should start with 12,500 CPAY:
        starting_balance = 12500
        for i in range(4):
            assert_equal(self.nodes[i].getbalance(), starting_balance)
            self.nodes[i].getnewaddress("")  # bug workaround, coins generated assigned to first getnewaddress!

        # Assign coins to foo and bar accounts:
        self.nodes[0].settxfee(.001)

        node0_address_foo = self.nodes[0].getnewaddress("foo")
        fund_foo_txid = self.nodes[0].sendfrom("", node0_address_foo, 12190)
        fund_foo_tx = self.nodes[0].gettransaction(fund_foo_txid)

        node0_address_bar = self.nodes[0].getnewaddress("bar")
        fund_bar_txid = self.nodes[0].sendfrom("", node0_address_bar, 290)
        fund_bar_tx = self.nodes[0].gettransaction(fund_bar_txid)

        assert_equal(self.nodes[0].getbalance(""),
                     starting_balance - 12190 - 290 + fund_foo_tx["fee"] + fund_bar_tx["fee"])

        # Coins are sent to node1_address
        node1_address = self.nodes[1].getnewaddress("from0")

        # Send tx1, and another transaction tx2 that won't be cloned 
        txid1 = self.nodes[0].sendfrom("foo", node1_address, 400, 0)
        txid2 = self.nodes[0].sendfrom("bar", node1_address, 200, 0)

        # Construct a clone of tx1, to be malleated 
        rawtx1 = self.nodes[0].getrawtransaction(txid1,1)
        clone_inputs = [{"txid":rawtx1["vin"][0]["txid"],"vout":rawtx1["vin"][0]["vout"]}]
        clone_outputs = {rawtx1["vout"][0]["scriptPubKey"]["addresses"][0]:rawtx1["vout"][0]["value"],
                         rawtx1["vout"][1]["scriptPubKey"]["addresses"][0]:rawtx1["vout"][1]["value"]}
        clone_raw = self.nodes[0].createrawtransaction(clone_inputs, clone_outputs)

        # 3 hex manipulations on the clone are required

        # manipulation 1. sequence is at version+#inputs+input+sigstub
        posseq = 2*(4+1+36+1)
        seqbe = '%08x' % rawtx1["vin"][0]["sequence"]
        clone_raw = clone_raw[:posseq] + seqbe[6:8] + seqbe[4:6] + seqbe[2:4] + seqbe[0:2] + clone_raw[posseq + 8:]

        # manipulation 2. createrawtransaction randomizes the order of its outputs, so swap them if necessary.
        # output 0 is at version+#inputs+input+sigstub+sequence+#outputs
        # 400 CPAY serialized is 00902f5009000000
        pos0 = 2*(4+1+36+1+4+1)
        hex400 = "00902f5009000000"
        output_len = 16 + 2 + 2 * int("0x" + clone_raw[pos0 + 16 : pos0 + 16 + 2], 0)
        if (rawtx1["vout"][0]["value"] == 400 and clone_raw[pos0 : pos0 + 16] != hex400 or
            rawtx1["vout"][0]["value"] != 400 and clone_raw[pos0 : pos0 + 16] == hex400):
            output0 = clone_raw[pos0 : pos0 + output_len]
            output1 = clone_raw[pos0 + output_len : pos0 + 2 * output_len]
            clone_raw = clone_raw[:pos0] + output1 + output0 + clone_raw[pos0 + 2 * output_len:]

        # manipulation 3. locktime is after outputs
        poslt = pos0 + 2 * output_len
        ltbe = '%08x' % rawtx1["locktime"]
        clone_raw = clone_raw[:poslt] + ltbe[6:8] + ltbe[4:6] + ltbe[2:4] + ltbe[0:2] + clone_raw[poslt + 8:]

        # Use a different signature hash type to sign.  This creates an equivalent but malleated clone.
        # Don't send the clone anywhere yet
        tx1_clone = self.nodes[0].signrawtransaction(clone_raw, None, None, "ALL|ANYONECANPAY")
        assert_equal(tx1_clone["complete"], True)

        # Have node0 mine a block, if requested:
        if (self.options.mine_block):
            self.nodes[0].generate(1)
            sync_blocks(self.nodes[0:2])

        tx1 = self.nodes[0].gettransaction(txid1)
        tx2 = self.nodes[0].gettransaction(txid2)

        # Node0's balance should be starting balance, plus 500CPAY for another
        # matured block, minus tx1 and tx2 amounts, and minus transaction fees:
        expected = starting_balance + fund_foo_tx["fee"] + fund_bar_tx["fee"]
        if self.options.mine_block: expected += 500
        expected += tx1["amount"] + tx1["fee"]
        expected += tx2["amount"] + tx2["fee"]
        assert_equal(self.nodes[0].getbalance(), expected)

        # foo and bar accounts should be debited:
        assert_equal(self.nodes[0].getbalance("foo", 0), 12190 + tx1["amount"] + tx1["fee"])
        assert_equal(self.nodes[0].getbalance("bar", 0), 290 + tx2["amount"] + tx2["fee"])

        if self.options.mine_block:
            assert_equal(tx1["confirmations"], 1)
            assert_equal(tx2["confirmations"], 1)
            # Node1's "from0" balance should be both transaction amounts:
            assert_equal(self.nodes[1].getbalance("from0"), -(tx1["amount"] + tx2["amount"]))
        else:
            assert_equal(tx1["confirmations"], 0)
            assert_equal(tx2["confirmations"], 0)

        # Send clone and its parent to miner
        self.nodes[2].sendrawtransaction(fund_foo_tx["hex"])
        txid1_clone = self.nodes[2].sendrawtransaction(tx1_clone["hex"])
        # ... mine a block...
        self.nodes[2].generate(1)

        # Reconnect the split network, and sync chain:
        connect_nodes(self.nodes[1], 2)
        self.nodes[2].sendrawtransaction(fund_bar_tx["hex"])
        self.nodes[2].sendrawtransaction(tx2["hex"])
        self.nodes[2].generate(1)  # Mine another block to make sure we sync
        sync_blocks(self.nodes)

        # Re-fetch transaction info:
        tx1 = self.nodes[0].gettransaction(txid1)
        tx1_clone = self.nodes[0].gettransaction(txid1_clone)
        tx2 = self.nodes[0].gettransaction(txid2)
        
        # Verify expected confirmations
        assert_equal(tx1["confirmations"], -2)
        assert_equal(tx1_clone["confirmations"], 2)
        assert_equal(tx2["confirmations"], 1)

        # Check node0's total balance; should be same as before the clone, + 1000 CPAY for 2 matured,
        # less possible orphaned matured subsidy
        expected += 1000
        if (self.options.mine_block): 
            expected -= 500
        assert_equal(self.nodes[0].getbalance(), expected)
        assert_equal(self.nodes[0].getbalance("*", 0), expected)

        # Check node0's individual account balances.
        # "foo" should have been debited by the equivalent clone of tx1
        assert_equal(self.nodes[0].getbalance("foo"), 12190 + tx1["amount"] + tx1["fee"])
        # "bar" should have been debited by (possibly unconfirmed) tx2
        assert_equal(self.nodes[0].getbalance("bar", 0), 290 + tx2["amount"] + tx2["fee"])
        # "" should have starting balance, less funding txes, plus subsidies
        assert_equal(self.nodes[0].getbalance("", 0), starting_balance
                                                                - 12190
                                                                + fund_foo_tx["fee"]
                                                                -   290
                                                                + fund_bar_tx["fee"]
                                                                +  1000)

        # Node1's "from0" account balance
        assert_equal(self.nodes[1].getbalance("from0", 0), -(tx1["amount"] + tx2["amount"]))

if __name__ == '__main__':
    TxnMallTest().main()

