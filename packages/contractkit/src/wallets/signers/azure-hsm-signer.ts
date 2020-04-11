import { ensureLeading0x, trimLeading0x } from '@celo/utils/lib/address'
import * as ethUtil from 'ethereumjs-util'
import { getHashFromEncoded, RLPEncodedTx } from '../../utils/signing-utils'
import { AzureKeyVaultClient } from './azure-key-vault-client'
import { Signer } from './signer'

/**
 * Signs the EVM transaction using an HSM key in Azure Key Vault
 */
export class AzureHSMSigner implements Signer {
  private keyVaultClient: AzureKeyVaultClient
  private keyName: string

  constructor(keyVaultClient: AzureKeyVaultClient, keyName: string) {
    this.keyVaultClient = keyVaultClient
    this.keyName = keyName
  }

  async signTransaction(
    addToV: number,
    encodedTx: RLPEncodedTx
  ): Promise<{ v: string; r: string; s: string }> {
    const hash = getHashFromEncoded(encodedTx.rlpEncode)
    const bufferedMessage = Buffer.from(trimLeading0x(hash), 'hex')
    const signature = await this.keyVaultClient.signMessage(bufferedMessage, this.keyName)
    const sigVT = addToV + signature.v
    return {
      v: sigVT.toString(16),
      r: signature.r.toString('hex'),
      s: signature.s.toString('hex'),
    }
  }

  async signPersonalMessage(data: string): Promise<{ v: number; r: Buffer; s: Buffer }> {
    const dataBuff = ethUtil.toBuffer(ensureLeading0x(data))
    const msgHashBuff = ethUtil.hashPersonalMessage(dataBuff)
    const signature = await this.keyVaultClient.signMessage(Buffer.from(msgHashBuff), this.keyName)
    const sigV = signature.v + 27

    return {
      v: sigV,
      r: signature.r,
      s: signature.s,
    }
  }

  getNativeKey(): string {
    return this.keyName
  }
}
