-- ==========================================
-- STEP 1: CONNECT TO YOUR TARGET DATABASE FIRST
-- ==========================================
-- If using psql, uncomment and run the line below, or connect via your IDE:
-- \c your_database_name

-- ==========================================
-- STEP 2: CREATE THE USER
-- ==========================================
CREATE USER ro_usr_captain_cookie WITH PASSWORD '';

-- ==========================================
-- STEP 3: GRANT DATABASE LAYER ACCESS
-- ==========================================
-- Allows the user to connect to the database
GRANT CONNECT ON DATABASE captain_cookie TO ro_usr_captain_cookie;

-- ==========================================
-- STEP 4: GRANT SCHEMA LAYER ACCESS
-- ==========================================
-- Allows the user to look up objects within the public schema
GRANT USAGE ON SCHEMA public TO ro_usr_captain_cookie;

-- ==========================================
-- STEP 5: GRANT OBJECT LAYER ACCESS (EXISTING OBJECTS)
-- ==========================================
-- Grant read-only access to all existing tables
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ro_usr_captain_cookie;

-- Grant access to sequences (necessary if they read views or serial columns)
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO ro_usr_captain_cookie;

-- Optional: Grant permission to execute existing functions
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO ro_usr_captain_cookie;

-- ==========================================
-- STEP 6: MAINTAIN READ-ONLY ACCESS FOR FUTURE TABLES
-- ==========================================
-- By default, new tables won't be readable. This ensures any future tables 
-- created by the current user (usually the migrations/admin user) are readable.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ro_usr_captain_cookie;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO ro_usr_captain_cookie;
