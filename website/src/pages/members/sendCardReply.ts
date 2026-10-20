import { api } from '../../api/client'
import { sendTurn, type SendWire } from '../../chat-core/transport/sendTurn'
import { mintSendId } from '../../utils/sendDelivery'

/** The fetch seam for a Dashboard card's reply: the usual `POST /api/chat?ws=1`, plus
 *  `origin: 'dashboard_card'`, which the gateway checks again and which tags the turn. */
const cardSendWire: SendWire = (payload, signal) =>
  api.sendChat(payload.message, payload.slot, undefined, signal, payload.meta, undefined, undefined, 'dashboard_card')

/**
 * Send a reply a Dashboard-tab button offered into the crewmate's own thread.
 *
 * Resolves `true` when the gateway took it (or may have: a late or unreadable
 * answer is not a refusal, and filling the composer then would invite a second
 * send), `false` when the text should be put in the chat box instead. The gateway
 * refuses a card send when the crewmate's Profile switch is off, the thread is busy
 * or waiting on an approval or a question, or a send went through moments ago.
 * None of those loses the reply: the caller fills the composer and the person
 * presses send, which is what a button did before one-click sends existed.
 */
export async function sendCardReply(slot: string, text: string): Promise<boolean> {
  const receipt = await sendTurn({
    message: text,
    slot,
    // Echoed back by the gateway, so the sent row appears in this tab.
    meta: { sendId: mintSendId() },
    wire: cardSendWire,
  })
  return receipt.status !== 'refused' && receipt.status !== 'transport-error'
}
