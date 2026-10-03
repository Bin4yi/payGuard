import { useId, useRef, useState } from "react";
import { Camera } from "./Icons";

export type PhotoMediaType = "image/png" | "image/jpeg" | "image/webp";

export interface PhotoResult {
  text: string;
  mediaType: PhotoMediaType;
  confidence: number;
}

/** Real notice images from Wikimedia Commons (see fixtures/real_notices/CREDITS.md). */
export const SAMPLE_PHOTOS: { label: string; url: string }[] = [
  { label: "Real fake-invoice scam", url: "/samples/fake-invoice-bluffaktura.jpg" },
  { label: "Real phishing email", url: "/samples/phishing-bank-email.png" },
  { label: "Printed poisoned notice", url: "/samples/poisoned-rent-notice.png" },
];

const ACCEPTED: PhotoMediaType[] = ["image/png", "image/jpeg", "image/webp"];
const MAX_BYTES = 8 * 1024 * 1024;

/**
 * Read a photo or screenshot of a notice with OCR, entirely in the browser.
 * Only the extracted text is ever sent to the server, and it is treated
 * exactly like typed text: untrusted, and unable to change the approval.
 */
export function PhotoNotice({ onText, disabled }: { onText: (r: PhotoResult) => void; disabled?: boolean }) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [status, setStatus] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const read = async (blob: Blob, name: string) => {
    setError(null);
    const type = blob.type as PhotoMediaType;
    if (!ACCEPTED.includes(type)) {
      setError("Use a PNG, JPEG or WebP image.");
      return;
    }
    if (blob.size > MAX_BYTES) {
      setError("That image is larger than 8 MB.");
      return;
    }
    setPreview(URL.createObjectURL(blob));
    setProgress(0);
    setStatus(`Reading ${name}…`);
    try {
      const { createWorker } = await import("tesseract.js");
      const worker = await createWorker("eng", 1, {
        logger: (m: { status: string; progress: number }) => {
          if (m.status === "recognizing text") setProgress(Math.round(m.progress * 100));
        },
      });
      const { data } = await worker.recognize(blob);
      await worker.terminate();
      const text = data.text.replace(/\n{3,}/g, "\n\n").trim();
      setProgress(100);
      if (!text) {
        setStatus("No readable text found (handwriting and blurry photos are hard for OCR).");
        return;
      }
      setStatus(`Read ${text.length} characters · OCR confidence ${Math.round(data.confidence)}%`);
      onText({ text, mediaType: type, confidence: data.confidence });
    } catch (err) {
      setProgress(null);
      setError(`Couldn't read the image: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const loadSample = async (url: string, label: string) => {
    const resp = await fetch(url);
    await read(await resp.blob(), label);
  };

  const busy = progress !== null && progress < 100;

  return (
    <div>
      <div
        className={`dropzone${dragging ? " dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const file = e.dataTransfer.files[0];
          if (file) void read(file, file.name);
        }}
      >
        {preview ? <img src={preview} alt="The uploaded notice" /> : <span className="icon-well"><Camera /></span>}
        <div className="grow">
          <label htmlFor={inputId} style={{ marginBottom: 0 }}>
            Upload a photo of the notice
          </label>
          <span className="muted small">
            Read on this device with OCR. Only the text is sent, and it can't change what you approve.
          </span>
          {progress !== null && (
            <div className="progress" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100} aria-label="Reading the image">
              <span style={{ width: `${progress}%` }} />
            </div>
          )}
          {status && <p className="small" role="status" style={{ margin: "0.4rem 0 0" }}>{status}</p>}
        </div>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept={ACCEPTED.join(",")}
          style={{ display: "none" }}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void read(file, file.name);
            e.target.value = "";
          }}
        />
        <button type="button" onClick={() => inputRef.current?.click()} disabled={disabled || busy}>
          Choose image
        </button>
      </div>
      <div className="sample-strip" aria-label="Sample images">
        <span className="muted small">Try a real image:</span>
        {SAMPLE_PHOTOS.map((s) => (
          <button key={s.url} type="button" onClick={() => void loadSample(s.url, s.label)} disabled={disabled || busy}>
            {s.label}
          </button>
        ))}
      </div>
      {error && (
        <div className="error-box" role="alert" style={{ marginTop: "0.75rem" }}>
          {error}
        </div>
      )}
    </div>
  );
}
