import React, { useEffect, useState } from 'react';
import { Play, Plus } from 'lucide-react';
import { callAction } from '../api';
import { FlashcardDeckWidget } from '../components/FlashcardDeckWidget';
import type { EducationCourse, FlashcardDeckPayload } from '../types';

interface KnowledgeStudyViewProps {
  onRefreshStatus: () => Promise<void>;
  onToast: (msg: string) => void;
}

export const KnowledgeStudyView: React.FC<KnowledgeStudyViewProps> = ({ onRefreshStatus, onToast }) => {
  const [courses, setCourses] = useState<EducationCourse[]>([]);
  const [studyDeck, setStudyDeck] = useState<FlashcardDeckPayload | null>(null);
  const [newCourseTitle, setNewCourseTitle] = useState('');

  const loadStudyState = async (courseId = '') => {
    const prog = await callAction<{ courses?: EducationCourse[]; active_course?: EducationCourse }>('education', {
      subaction: 'progress',
    });
    const list = prog.courses || [];
    setCourses(list);
    const targetId = courseId || prog.active_course?.course_id || list[0]?.course_id || '';
    if (targetId) {
      const dRes = await callAction<{ ok?: boolean; deck?: FlashcardDeckPayload }>('education', {
        subaction: 'flashcard_deck',
        course_id: targetId,
        limit: 8,
      });
      setStudyDeck(dRes.ok && dRes.deck ? dRes.deck : null);
    } else {
      setStudyDeck(null);
    }
  };

  useEffect(() => {
    void loadStudyState();
  }, []);

  const createQuickCourse = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newCourseTitle.trim()) return;
    const res = await callAction('education', {
      subaction: 'save_course',
      title: newCourseTitle.trim(),
      set_active: true,
    });
    if (res.ok) {
      setNewCourseTitle('');
      await loadStudyState();
      await onRefreshStatus();
      onToast(res.summary || 'Created study course.');
    }
  };

  return (
    <div className="surface-scroll">
      <div className="surface-container" style={{ maxWidth: 780 }}>
        <form onSubmit={(e) => void createQuickCourse(e)} style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
          <input
            className="input"
            placeholder="Start a new study course (e.g. Rust Ownership, ZFS Storage)..."
            value={newCourseTitle}
            onChange={(e) => setNewCourseTitle(e.target.value)}
          />
          <button type="submit" className="btn btn-primary btn-sm" style={{ flexShrink: 0 }}>
            <Plus size={12} /> Add Course
          </button>
        </form>

        {studyDeck && studyDeck.cards.length > 0 && (
          <FlashcardDeckWidget
            deck={studyDeck}
            onGraded={() => {
              void loadStudyState(studyDeck.course_id);
              void onRefreshStatus();
            }}
          />
        )}
        <div className="card-group" style={{ marginTop: 14 }}>
          {courses.length === 0 ? (
            <div className="card-row">
              <span style={{ color: 'var(--text-muted)' }}>
                No active courses yet — add a topic above or ask Companion in Chat to build a course with flashcards!
              </span>
            </div>
          ) : (
            courses.map((c) => (
              <div key={c.course_id} className="card-row">
                <div className="card-row-info">
                  <div className="card-row-title">{c.title}</div>
                  <div className="card-row-desc">
                    {c.mastered_modules}/{c.total_modules} modules • {c.total_items} card(s) ({c.due_count} due)
                  </div>
                </div>
                <div style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                  <span className="badge accent">{c.mastery_pct}% mastery</span>
                  {c.total_items > 0 && (
                    <button type="button" className="btn btn-sm" onClick={() => void loadStudyState(c.course_id)}>
                      <Play size={11} /> Deck
                    </button>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
