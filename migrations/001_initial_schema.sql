-- migrations/001_initial_schema.sql
-- Schema inicial para o canal Mobile do CERCO

-- Tabela de usuários
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_created_at ON users(created_at DESC);

-- Tabela de alerts (oportunidades de arbitragem)
CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    match_data JSONB NOT NULL,
    opportunities JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_alerts_created_at ON alerts(created_at DESC);
CREATE INDEX idx_alerts_match_data ON alerts USING GIN(match_data);

-- Tabela de tokens de dispositivos para push notifications
CREATE TABLE IF NOT EXISTS device_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token VARCHAR(512) NOT NULL UNIQUE,
    platform VARCHAR(50) NOT NULL DEFAULT 'expo',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_device_tokens_user_id ON device_tokens(user_id);
CREATE INDEX idx_device_tokens_token ON device_tokens(token);
CREATE INDEX idx_device_tokens_created_at ON device_tokens(created_at DESC);

-- Comentários para documentação
COMMENT ON TABLE users IS 'Usuários cadastrados no aplicativo mobile';
COMMENT ON TABLE alerts IS 'Oportunidades de arbitragem detectadas pelo CERCO';
COMMENT ON TABLE device_tokens IS 'Tokens de push notification dos dispositivos móveis';

COMMENT ON COLUMN alerts.match_data IS 'Dados do confronto (snapshot) em formato JSON';
COMMENT ON COLUMN alerts.opportunities IS 'Lista de oportunidades de arbitragem em formato JSON';
COMMENT ON COLUMN device_tokens.token IS 'Expo Push Token do dispositivo';
COMMENT ON COLUMN device_tokens.platform IS 'Plataforma do dispositivo (expo, ios, android)';
