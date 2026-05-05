-- Adds nullable advances_goal soft-FK column to events.
-- Formal REFERENCES goals(goal_id) constraint will be added by T-PM-01 during the events table rebuild for XES extension.
ALTER TABLE events ADD COLUMN advances_goal TEXT;
CREATE INDEX idx_events_advances_goal ON events(advances_goal) WHERE advances_goal IS NOT NULL;
