import React, { useEffect, useState } from 'react';
import { Play, Plus, Sparkles } from 'lucide-react';
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
  const [creating, setCreating] = useState(false);
  const [generatingKey, setGeneratingKey] = useState('');

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
    if (!newCourseTitle.trim() || creating) return;
    setCreating(true);
    const res = await callAction<{ course_id?: string; summary?: string; error?: string }>('education', {
      subaction: 'save_course',
      title: newCourseTitle.trim(),
      set_active: true,
    });
    setCreating(false);
    if (res.ok) {
      setNewCourseTitle('');
      await loadStudyState(res.course_id || '');
      await onRefreshStatus();
      onToast(res.summary || 'Created study course with AI syllabus.');
    } else {
      onToast(res.error || 'Could not create course.');
    }
  };

  const generateItems = async (courseId: string, itemKind: 'flashcard' | 'quiz') => {
    const key = `${courseId}:${itemKind}`;
    if (generatingKey) return;
    setGeneratingKey(key);
    const res = await callAction<{ deck?: FlashcardDeckPayload; summary?: string; error?: string }>('education', {
      subaction: 'generate_items',
      course_id: courseId,
      item_kind: itemKind,
      count: 5,
    });
    setGeneratingKey('');
    if (res.ok) {
      if (res.deck) {
        setStudyDeck(res.deck);
      }
      await loadStudyState(courseId);
      await onRefreshStatus();
      onToast(res.summary || `Generated ${itemKind} deck.`);
    } else {
      onToast(res.error || `Could not generate ${itemKind} items.`);
    }
  };

  return (
    <div className="surface-scroll" data-testid="knowledge-study-view">
      <div className="surface-container" style={{ maxWidth: 780 }}>
        <form onSubmit={(e) => void createQuickCourse(e)} style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
          <input
            className="input"
            data-testid="study-new-course-input"
            placeholder="Start a new study course (e.g. French Sauce Fundamentals, Rust Ownership)..."
            value={newCourseTitle}
            onChange={(e) => setNewCourseTitle(e.target.value)}
          />
          <button
            type="submit"
            className="btn btn-primary btn-sm"
            data-testid="study-add-course-btn"
            disabled={creating}
            style={{ flexShrink: 0 }}
          >
            <Plus size={12} /> {creating ? 'Designing Syllabus...' : 'Add Course'}
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
                No active courses yet — add a topic above to generate a multi-module syllabus and active-recall deck!
              </span>
            </div>
          ) : (
            courses.map((c) => (
              <div key={c.course_id} className="card-row" style={{ alignItems: 'flex-start', flexWrap: 'wrap', gap: 10 }}>
                <div className="card-row-info" style={{ minWidth: 220 }}>
                  <div className="card-row-title">{c.title}</div>
                  <div className="card-row-desc">
                    {c.mastered_modules}/{c.total_modules} modules • {c.total_items} card(s) ({c.due_count} due)
                    {c.goal_summary ? ` • ${c.goal_summary}` : ''}
                  </div>
                  {Array.isArray(c.modules) && c.modules.length > 0 && (
                    <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginTop: 6 }}>
                      {c.modules.map((m, idx) => (
                        <span
                          key={m.slug || idx}
                          className={`badge ${idx === c.current_module_idx ? 'accent' : ''}`}
                          title={m.objective || m.title}
                        >
                          {idx + 1}. {m.title}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div style={{ display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                  <span className="badge accent">{c.mastery_pct}% mastery</span>
                  <button
                    type="button"
                    className="btn btn-sm"
                    data-testid={`study-generate-flashcards-${c.course_id}`}
                    disabled={Boolean(generatingKey)}
                    onClick={() => void generateItems(c.course_id, 'flashcard')}
                  >
                    <Sparkles size={11} />{' '}
                    {generatingKey === `${c.course_id}:flashcard` ? 'Generating...' : 'Generate Flashcards'}
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    data-testid={`study-generate-quiz-${c.course_id}`}
                    disabled={Boolean(generatingKey)}
                    onClick={() => void generateItems(c.course_id, 'quiz')}
                  >
                    <Sparkles size={11} /> {generatingKey === `${c.course_id}:quiz` ? 'Generating...' : 'Generate Quiz'}
                  </button>
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
