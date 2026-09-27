import { useState, useEffect, type FormEvent } from 'react'
import { Modal } from '../common/Modal'
import { useMailStore } from '../../stores/mailStore'
import './ComposeModal.css'

interface ComposeModalProps {
  open: boolean
  onClose: () => void
  replyTo?: { to_agent: string; subject: string; thread_id?: string | null }
}

export function ComposeModal({ open, onClose, replyTo }: ComposeModalProps) {
  const { sendMail } = useMailStore()
  const [to, setTo] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [sending, setSending] = useState(false)

  // Reset form when modal opens/closes or replyTo changes
  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset the form each time the modal opens
      setTo(replyTo?.to_agent || '')
      setSubject(replyTo ? `Re: ${replyTo.subject}` : '')
      setBody('')
      setSending(false)
    }
  }, [open, replyTo])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!to.trim() || !subject.trim() || !body.trim()) return
    setSending(true)
    const ok = await sendMail({
      to_agent: to.trim(),
      subject: subject.trim(),
      body: body.trim(),
      thread_id: replyTo?.thread_id || undefined,
    })
    setSending(false)
    if (ok) {
      onClose()
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Compose" width="560px">
      <form className="compose-form" onSubmit={handleSubmit}>
        <div className="compose-field">
          <label>To</label>
          <input
            type="text"
            value={to}
            onChange={e => setTo(e.target.value)}
            placeholder="Agent name..."
            autoFocus
          />
        </div>
        <div className="compose-field">
          <label>Subject</label>
          <input
            type="text"
            value={subject}
            onChange={e => setSubject(e.target.value)}
            placeholder="Subject..."
          />
        </div>
        <div className="compose-field">
          <label>Body</label>
          <textarea
            value={body}
            onChange={e => setBody(e.target.value)}
            placeholder="Write your message..."
            rows={8}
          />
        </div>
        <div className="compose-actions">
          <button type="button" className="compose-cancel" onClick={onClose}>Cancel</button>
          <button
            type="submit"
            className="compose-send"
            disabled={sending || !to.trim() || !subject.trim() || !body.trim()}
          >
            {sending ? 'Sending...' : 'Send'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
