-- Public, synthetic demonstration credentials for the loopback-only local stack.
-- Never apply this fixture to staging/production. Password: local-only-password.
INSERT INTO public.user_groups(slug, label, icon, color, priority, password_hash)
VALUES ('local-demo', 'Local demo', '🧪', '#0053b3', 10, 'pbkdf2_sha256$200000$bG9jYWwtZGVtby1zYWx0IQ==$i7LbqBFeAS1oLT0elwKFCUnyDsu9zYYs0gNPFL7+aW4=')
ON CONFLICT (slug) DO NOTHING;
