export interface MailMessage {
  id: string | number
  from_agent: string
  to_agent: string
  subject: string
  body: string
  timestamp: string
  read: boolean
  archived: boolean
  thread_id: string | null
}

export interface SendMailRequest {
  to_agent: string
  subject: string
  body: string
  thread_id?: string
}

export interface UpdateMailRequest {
  read?: boolean
  archived?: boolean
}
