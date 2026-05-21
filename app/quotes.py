"""
Curated motivational quotes shown in the empty space alongside the
invoice form (and other places that benefit from a tiny moment of pep).
Picked for a small-business / contractor / entrepreneur audience —
short, punchy, action-oriented. No filler.
"""
from __future__ import annotations

import random


QUOTES: list[dict] = [
    {"text": "Do what you can, with what you have, where you are.", "author": "Theodore Roosevelt"},
    {"text": "Quality is not an act, it is a habit.", "author": "Aristotle"},
    {"text": "The way to get started is to quit talking and begin doing.", "author": "Walt Disney"},
    {"text": "Success is not final, failure is not fatal: it is the courage to continue that counts.", "author": "Winston Churchill"},
    {"text": "Don't watch the clock; do what it does. Keep going.", "author": "Sam Levenson"},
    {"text": "The harder I work, the luckier I get.", "author": "Samuel Goldwyn"},
    {"text": "Hard work beats talent when talent doesn't work hard.", "author": "Tim Notke"},
    {"text": "Excellence is the gradual result of always striving to do better.", "author": "Pat Riley"},
    {"text": "Your work is going to fill a large part of your life, and the only way to be truly satisfied is to do what you believe is great work.", "author": "Steve Jobs"},
    {"text": "It always seems impossible until it's done.", "author": "Nelson Mandela"},
    {"text": "Discipline is the bridge between goals and accomplishment.", "author": "Jim Rohn"},
    {"text": "Either you run the day, or the day runs you.", "author": "Jim Rohn"},
    {"text": "Stay focused, go after your dreams and keep moving toward your goals.", "author": "LL Cool J"},
    {"text": "The future depends on what you do today.", "author": "Mahatma Gandhi"},
    {"text": "Success usually comes to those who are too busy to be looking for it.", "author": "Henry David Thoreau"},
    {"text": "Don't be afraid to give up the good to go for the great.", "author": "John D. Rockefeller"},
    {"text": "The only way to do great work is to love what you do.", "author": "Steve Jobs"},
    {"text": "Action is the foundational key to all success.", "author": "Pablo Picasso"},
    {"text": "It does not matter how slowly you go as long as you do not stop.", "author": "Confucius"},
    {"text": "Do not be embarrassed by your failures, learn from them and start again.", "author": "Richard Branson"},
    {"text": "Whether you think you can, or you think you can't — you're right.", "author": "Henry Ford"},
    {"text": "Believe you can and you're halfway there.", "author": "Theodore Roosevelt"},
    {"text": "Build your own dreams, or someone else will hire you to build theirs.", "author": "Farrah Gray"},
    {"text": "Success is walking from failure to failure with no loss of enthusiasm.", "author": "Winston Churchill"},
    {"text": "Working hard for something we don't care about is called stress. Working hard for something we love is called passion.", "author": "Simon Sinek"},
    {"text": "Opportunity is missed by most people because it is dressed in overalls and looks like work.", "author": "Thomas Edison"},
    {"text": "Pleasure in the job puts perfection in the work.", "author": "Aristotle"},
    {"text": "Small daily improvements over time lead to stunning results.", "author": "Robin Sharma"},
    {"text": "Talent is cheaper than table salt. What separates the talented individual from the successful one is a lot of hard work.", "author": "Stephen King"},
    {"text": "Don't count the days, make the days count.", "author": "Muhammad Ali"},
    {"text": "You miss 100% of the shots you don't take.", "author": "Wayne Gretzky"},
    {"text": "The expert in anything was once a beginner.", "author": "Helen Hayes"},
    {"text": "If you want to make your dreams come true, the first thing you have to do is wake up.", "author": "J.M. Power"},
    {"text": "The secret of getting ahead is getting started.", "author": "Mark Twain"},
    {"text": "Wake up determined, go to bed satisfied.", "author": "Anonymous"},
    {"text": "Be the kind of person your dog thinks you are.", "author": "Anonymous"},
    {"text": "When everything seems to be going against you, remember that the airplane takes off against the wind, not with it.", "author": "Henry Ford"},
    {"text": "If you're not willing to risk the usual, you will have to settle for the ordinary.", "author": "Jim Rohn"},
    {"text": "Success is liking yourself, liking what you do, and liking how you do it.", "author": "Maya Angelou"},
    {"text": "What you get by achieving your goals is not as important as what you become by achieving your goals.", "author": "Zig Ziglar"},
    {"text": "The best preparation for tomorrow is doing your best today.", "author": "H. Jackson Brown Jr."},
    {"text": "A year from now you may wish you had started today.", "author": "Karen Lamb"},
    {"text": "I have not failed. I've just found 10,000 ways that won't work.", "author": "Thomas Edison"},
    {"text": "Strive not to be a success, but rather to be of value.", "author": "Albert Einstein"},
    {"text": "Don't let yesterday take up too much of today.", "author": "Will Rogers"},
]


def random_quote() -> dict:
    """One quote, chosen uniformly at random."""
    return random.choice(QUOTES)
