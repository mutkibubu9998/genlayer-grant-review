/* Direct StudioNet deploy of GrantReview using genlayer-js (bypasses the
 * `genlayer` CLI, whose deploy command hangs after printing the deployment
 * parameters). Mirrors the CLI's deploy flow step for step, with a timestamp
 * log line before/after every await so a hang is diagnosable.
 * Password is read from the file at runtime and never printed.
 */
const fs = require("fs");
const path = require("path");
const { ethers } = require("/usr/lib/node_modules/genlayer/node_modules/ethers");
const {
  createClient,
  createAccount,
} = require("/usr/lib/node_modules/genlayer/node_modules/genlayer-js");
const {
  studionet,
} = require("/usr/lib/node_modules/genlayer/node_modules/genlayer-js/dist/chains/index.js");

const HOME = process.env.HOME;
const CONTRACT = path.join(HOME, "workspace/genlayer/grant-review/contracts/grant_review.py");

function log(msg) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`TIMEOUT after ${ms}ms: ${label}`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function main() {
  const pw = fs
    .readFileSync(path.join(HOME, "workspace/genlayer/.builder-account-password"), "utf8")
    .replace(/[\r\n]+$/, "");
  const keystoreJson = fs.readFileSync(
    path.join(HOME, ".genlayer/keystores/genlayer-builder.json"),
    "utf8"
  );

  log("decrypting keystore...");
  const wallet = await withTimeout(
    ethers.Wallet.fromEncryptedJson(keystoreJson, pw),
    60000,
    "decrypt keystore"
  );
  log(`account: ${wallet.address}`);

  const account = createAccount(wallet.privateKey);
  const client = createClient({ chain: studionet, account });
  log(`client created, chain: ${studionet.name} id=${studionet.id}`);

  log("getCurrentNonce...");
  const nonce = await withTimeout(
    client.getCurrentNonce({ address: account.address }),
    60000,
    "getCurrentNonce"
  );
  log(`nonce: ${nonce}`);

  const code = fs.readFileSync(CONTRACT, "utf8");
  log(`contract code read: ${code.length} chars`);

  log("deployContract...");
  const hash = await withTimeout(
    client.deployContract({ code, args: [], leaderOnly: false }),
    300000,
    "deployContract"
  );
  log(`DEPLOY_TX_HASH: ${hash}`);

  log("waitForTransactionReceipt (ACCEPTED)...");
  const receipt = await withTimeout(
    client.waitForTransactionReceipt({
      hash,
      retries: 100,
      interval: 5000,
      status: "ACCEPTED",
    }),
    600000,
    "waitForTransactionReceipt"
  );
  log(`RECEIPT: ${JSON.stringify(receipt).slice(0, 3000)}`);
  const contractAddress =
    receipt?.data?.contract_address ?? receipt?.txDataDecoded?.contractAddress ?? null;
  log(`CONTRACT_ADDRESS: ${contractAddress}`);
}

main().catch((e) => {
  console.error(`FAILED: ${e && e.stack ? e.stack : e}`);
  process.exit(1);
});
