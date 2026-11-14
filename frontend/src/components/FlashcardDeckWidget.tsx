import React, { useEffect, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, GraduationCap, RotateCcw, X } from 'lucide-react';
import { callAction } from '../api';
import type { FlashcardDeckPayload } from '../types';

interface FlashcardDeckWidgetProps {
  deck: FlashcardDeckPayload;
  onGraded?: () => void;
}

export const FlashcardDeckWidget: React.FC<FlashcardDeckWidgetProps> = ({ deck, onGraded }) => {
  const cards = Array.isArray(deck?.cards) ? deck.cards : [];
  const [index, setIndex] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [grading, setGrading] = useState(false);
  const [grades, setGrades] = useState<Record<string, 'pass' | 'miss'>>({});
  const [boxes, setBoxes] = useState<Record<string, number>>(() => {
    const init: Record<string, number> = {};
    for (const c of cards) {
      init[c.item_id] = typeof c.srs_box === 'number' ? c.srs_box : 0;
    }
    return init;
  });
  const [lastSummary, setLastSummary] = useState('');

  useEffect(() => {
    const ids = cards.map((c) => c.item_id).filter(Boolean);
    if (ids.length === 0) return;
    let cancelled = false;
    void callAction<{ items?: Array<{ item_id: string; srs_box: number }> }>('education', {
      subaction: 'get_items',
      item_ids: ids,
    })
      .then((res) => {
        if (cancelled || !res?.ok || !Array.isArray(res.items)) return;
        setBoxes((prev) => {
          const next = { ...prev };
          for (const it of res.items!) {
            if (it?.item_id && typeof it.srs_box === 'number') {
              next[it.item_id] = it.srs_box;
            }
          }
          return next;
        });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [cards.map((c) => c.item_id).join(',')]);

  if (cards.length === 0) return null;

  const safeIndex = Math.max(0, Math.min(cards.length - 1, index));
  const card = cards[safeIndex];
  const currentBox = boxes[card.item_id] ?? card.srs_box ?? 0;
  const currentGrade = grades[card.item_id];

  const gradedCount = Object.keys(grades).length;
  const passCount = Object.values(grades).filter((g) => g === 'pass').length;
  const missCount = Object.values(grades).filter((g) => g === 'miss').length;

  const goCard = (nextIdx: number) => {
    const clamped = Math.max(0, Math.min(cards.length - 1, nextIdx));
    setIndex(clamped);
    setFlipped(false);
  };

  const gradeCurrent = async (passed: boolean) => {
    if (grading) return;
    setGrading(true);
    try {
      const res = await callAction<{ srs_box?: number; summary?: string }>('education', {
        subaction: 'grade_item',
        item_id: card.item_id,
        passed,
      });
      if (res.ok) {
        setGrades((prev) => ({ ...prev, [card.item_id]: passed ? 'pass' : 'miss' }));
        if (typeof res.srs_box === 'number') {
          setBoxes((prev) => ({ ...prev, [card.item_id]: res.srs_box! }));
        }
        setLastSummary(res.summary || (passed ? 'Recorded pass.' : 'Scheduled for review.'));
        onGraded?.();
        if (safeIndex + 1 < cards.length) {
          setIndex(safeIndex + 1);
          setFlipped(false);
        }
      }
    } finally {
      setGrading(false);
    }
  };

  return (
    <div className="flashcard-widget" data-testid="flashcard-deck-widget">
      <div className="flashcard-head">
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
          <GraduationCap size={14} style={{ color: 'var(--accent-primary)', flexShrink: 0 }} />
          <strong style={{ fontSize: 12.5 }}>{deck.deck_title || 'Active Recall Deck'}</strong>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span className="badge mono">
            CARD {safeIndex + 1} / {cards.length}
          </span>
          <span className="badge accent mono">SRS BOX {currentBox}</span>
          {currentGrade && (
            <span className={`badge ${currentGrade === 'pass' ? 'ok' : 'warn'}`}>
              {currentGrade === 'pass' ? 'PASSED' : 'REVIEW'}
            </span>
          )}
        </div>
      </div>

      <button
        type="button"
        className={`flashcard-surface ${flipped ? 'is-flipped' : ''}`}
        onClick={() => setFlipped((f) => !f)}
        aria-label={flipped ? 'Show flashcard prompt' : 'Flip flashcard to reveal answer'}
      >
        <div className="flashcard-face-tag mono">
          <span>{flipped ? 'ANSWER' : 'PROMPT'}</span>
          {card.topic && <span>TOPIC: {card.topic}</span>}
        </div>
        <div className="flashcard-main-text">{flipped ? card.answer : card.prompt}</div>
        {flipped && card.explanation && (
          <div className="flashcard-explanation">{card.explanation}</div>
        )}
        <div className="flashcard-flip-hint mono">
          <RotateCcw size={11} /> {flipped ? 'Click to view prompt' : 'Click card to reveal answer'}
        </div>
      </button>

      <div className="flashcard-controls">
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <button
            type="button"
            className="btn"
            disabled={safeIndex === 0}
            onClick={() => goCard(safeIndex - 1)}
          >
            <ChevronLeft size={13} /> Prev
          </button>
          <button type="button" className="btn" onClick={() => setFlipped((f) => !f)}>
            <RotateCcw size={12} /> {flipped ? 'Show Prompt' : 'Show Answer'}
          </button>
          <button
            type="button"
            className="btn"
            disabled={safeIndex >= cards.length - 1}
            onClick={() => goCard(safeIndex + 1)}
          >
            Next <ChevronRight size={13} />
          </button>
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <button
            type="button"
            className="btn btn-danger"
            disabled={grading}
            onClick={() => void gradeCurrent(false)}
          >
            <X size={13} /> Missed It (Review)
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={grading}
            onClick={() => void gradeCurrent(true)}
          >
            <Check size={13} /> Got It (Pass)
          </button>
        </div>
      </div>

      {(gradedCount > 0 || lastSummary) && (
        <div className="flashcard-footer mono">
          <span>
            GRADED {gradedCount}/{cards.length} • PASS: {passCount} • REVIEW: {missCount}
          </span>
          {lastSummary && <span>{lastSummary}</span>}
        </div>
      )}
    </div>
  );
};
