import './LoadingSpinner.css'

export function LoadingSpinner({ size = 24 }: { size?: number }) {
  return (
    <div className="spinner" style={{ width: size, height: size }}>
      <div className="spinner-ring" />
    </div>
  )
}

export function FullPageSpinner() {
  return (
    <div className="spinner-fullpage">
      <LoadingSpinner size={40} />
    </div>
  )
}
