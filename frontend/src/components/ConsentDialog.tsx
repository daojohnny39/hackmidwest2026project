import { useEffect, useRef, useState } from 'react'
import { Icon } from './Artwork'
export function ConsentDialog({ onAccept, onClose }: { onAccept: () => void; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [accepted, setAccepted] = useState(false)
  useEffect(() => {
    const dialog = dialogRef.current
    const previous = document.activeElement as HTMLElement | null
    dialog?.showModal()
    return () => { dialog?.close(); previous?.focus() }
  }, [])
  return <dialog className="consent-dialog" ref={dialogRef} onCancel={onClose} aria-labelledby="consent-title" aria-describedby="consent-description">
    <button className="icon-button dialog-close" onClick={onClose} aria-label="Close consent prompt"><Icon name="close" /></button>
    <div className="consent-symbol"><Icon name="shield" /></div>
    <h2 id="consent-title">Camera consent</h2><p id="consent-description" className="consent-intro">Before you start:</p>
    <ol className="consent-terms">
      <li><span>01</span><p>Capture and analyze your own photos.</p></li>
      <li><span>02</span><p>Public-source search is not connected yet.</p></li>
      <li><span>03</span><p>Photos stay in this tab. Clear or reload to delete.</p></li>
      <li><span>04</span><p>Use responsibly, with consent.</p></li>
    </ol>
    <label className="consent-check"><input type="checkbox" checked={accepted} onChange={event => setAccepted(event.target.checked)} /><span>I agree to these terms, camera access, and local face analysis.</span></label>
    <div className="consent-actions"><button className="text-button" onClick={onClose}>Cancel</button><button className="button primary" disabled={!accepted} onClick={onAccept}>Agree & open camera <Icon name="arrow" /></button></div>
  </dialog>
}
