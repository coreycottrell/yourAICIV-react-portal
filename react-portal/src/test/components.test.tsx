import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LoadingSpinner, FullPageSpinner } from '../components/common/LoadingSpinner'
import { EmptyState } from '../components/common/EmptyState'
import { Modal } from '../components/common/Modal'
import { StatusBadge } from '../components/common/StatusBadge'
import { AuthModal } from '../components/auth/AuthModal'

describe('LoadingSpinner', () => {
  it('renders with default size', () => {
    const { container } = render(<LoadingSpinner />)
    const spinner = container.querySelector('.spinner')
    expect(spinner).toBeInTheDocument()
    expect(spinner).toHaveStyle({ width: '24px', height: '24px' })
  })

  it('renders with custom size', () => {
    const { container } = render(<LoadingSpinner size={40} />)
    const spinner = container.querySelector('.spinner')
    expect(spinner).toHaveStyle({ width: '40px', height: '40px' })
  })
})

describe('FullPageSpinner', () => {
  it('renders centered spinner', () => {
    const { container } = render(<FullPageSpinner />)
    expect(container.querySelector('.spinner-fullpage')).toBeInTheDocument()
    expect(container.querySelector('.spinner')).toBeInTheDocument()
  })
})

describe('EmptyState', () => {
  it('renders title', () => {
    render(<EmptyState title="No items" />)
    expect(screen.getByText('No items')).toBeInTheDocument()
  })

  it('renders description when provided', () => {
    render(<EmptyState title="Empty" description="Nothing here" />)
    expect(screen.getByText('Nothing here')).toBeInTheDocument()
  })

  it('renders icon when provided', () => {
    render(<EmptyState title="Empty" icon="📭" />)
    expect(screen.getByText('📭')).toBeInTheDocument()
  })

  it('renders action when provided', () => {
    render(<EmptyState title="Empty" action={<button>Create</button>} />)
    expect(screen.getByText('Create')).toBeInTheDocument()
  })
})

describe('Modal', () => {
  it('does not render when closed', () => {
    const { container } = render(
      <Modal open={false} onClose={() => {}}>Content</Modal>
    )
    expect(container.querySelector('.modal-overlay')).not.toBeInTheDocument()
  })

  it('renders when open', () => {
    render(
      <Modal open={true} onClose={() => {}}>Content</Modal>
    )
    expect(screen.getByText('Content')).toBeInTheDocument()
  })

  it('renders title when provided', () => {
    render(
      <Modal open={true} onClose={() => {}} title="Test Modal">Content</Modal>
    )
    expect(screen.getByText('Test Modal')).toBeInTheDocument()
  })

  it('calls onClose when clicking overlay', () => {
    const onClose = vi.fn()
    render(
      <Modal open={true} onClose={onClose}>Content</Modal>
    )
    const overlay = document.querySelector('.modal-overlay')!
    fireEvent.click(overlay)
    expect(onClose).toHaveBeenCalled()
  })

  it('calls onClose on Escape key', () => {
    const onClose = vi.fn()
    render(
      <Modal open={true} onClose={onClose}>Content</Modal>
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('does not close when clicking modal content', () => {
    const onClose = vi.fn()
    render(
      <Modal open={true} onClose={onClose}>Content</Modal>
    )
    const content = document.querySelector('.modal-content')!
    fireEvent.click(content)
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('StatusBadge', () => {
  it('renders online status', () => {
    const { container } = render(<StatusBadge status="online" label="Active" />)
    expect(container.querySelector('.status-online')).toBeInTheDocument()
    expect(screen.getByText('Active')).toBeInTheDocument()
  })

  it('renders offline status without label', () => {
    const { container } = render(<StatusBadge status="offline" />)
    expect(container.querySelector('.status-offline')).toBeInTheDocument()
  })
})

describe('AuthModal', () => {
  it('renders login form', () => {
    render(
      <MemoryRouter>
        <AuthModal />
      </MemoryRouter>
    )
    expect(screen.getByText('Sign in to your AI')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Paste your access code')).toBeInTheDocument()
    expect(screen.getByText('Sign in')).toBeInTheDocument()
  })

  it('disables submit when input is empty', () => {
    render(
      <MemoryRouter>
        <AuthModal />
      </MemoryRouter>
    )
    const submit = screen.getByText('Sign in')
    expect(submit).toBeDisabled()
  })

  it('enables submit when token entered', () => {
    render(
      <MemoryRouter>
        <AuthModal />
      </MemoryRouter>
    )
    const input = screen.getByPlaceholderText('Paste your access code')
    fireEvent.change(input, { target: { value: 'my-token' } })
    const submit = screen.getByText('Sign in')
    expect(submit).not.toBeDisabled()
  })
})
