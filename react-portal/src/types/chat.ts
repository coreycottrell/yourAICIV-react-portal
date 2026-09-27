export interface ChatMessage {
  id: string
  text: string
  role: 'user' | 'assistant'
  timestamp: number
  created_at?: string
  reactions?: Reaction[]
}

export interface Reaction {
  emoji: string
  count: number
  mine: boolean
}

export interface ChatHistoryResponse {
  messages: ChatMessage[]
}

export interface SendMessageRequest {
  message: string
}

export interface ReactionRequest {
  msg_id: string
  emoji: string
  action: 'add' | 'remove'
  msg_preview: string
  msg_role: 'user' | 'assistant'
}
