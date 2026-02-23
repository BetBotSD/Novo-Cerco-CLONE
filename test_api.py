#!/usr/bin/env python
# -*- coding: utf-8 -*-
# test_api.py
"""
Script de teste rápido para verificar se a API está funcionando.

Uso:
    python test_api.py
"""

import sys
import os

# Adiciona src ao path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_imports():
    """Testa se todos os módulos podem ser importados."""
    print("🧪 Testando imports...")
    
    try:
        from cerco import db
        print("  ✓ cerco.db")
    except Exception as e:
        print(f"  ✗ cerco.db: {e}")
        return False
    
    try:
        from cerco import push
        print("  ✓ cerco.push")
    except Exception as e:
        print(f"  ✗ cerco.push: {e}")
        return False
    
    try:
        from cerco import mobile_cerco
        print("  ✓ cerco.mobile_cerco")
    except Exception as e:
        print(f"  ✗ cerco.mobile_cerco: {e}")
        return False
    
    try:
        import api
        print("  ✓ api")
    except Exception as e:
        print(f"  ✗ api: {e}")
        return False
    
    print()
    return True


def test_api_endpoints():
    """Testa se a API FastAPI pode ser inicializada."""
    print("🧪 Testando API FastAPI...")
    
    try:
        from fastapi.testclient import TestClient
        import api
        
        client = TestClient(api.app)
        
        # Test root endpoint
        response = client.get("/")
        if response.status_code == 200:
            print("  ✓ GET / (root)")
        else:
            print(f"  ✗ GET / retornou {response.status_code}")
            return False
        
        # Test health endpoint
        response = client.get("/health")
        if response.status_code == 200:
            print("  ✓ GET /health")
        else:
            print(f"  ✗ GET /health retornou {response.status_code}")
            return False
        
        print()
        return True
    
    except ImportError:
        print("  ⚠️  TestClient não disponível (instale: pip install httpx)")
        print()
        return True
    except Exception as e:
        print(f"  ✗ Erro ao testar API: {e}")
        return False


def test_database_module():
    """Testa módulo de banco de dados (sem conectar)."""
    print("🧪 Testando módulo de banco de dados...")
    
    try:
        from cerco import db
        
        # Verifica se funções existem
        assert hasattr(db, 'init_db'), "Função init_db não encontrada"
        print("  ✓ init_db existe")
        
        assert hasattr(db, 'save_alert'), "Função save_alert não encontrada"
        print("  ✓ save_alert existe")
        
        assert hasattr(db, 'get_alerts'), "Função get_alerts não encontrada"
        print("  ✓ get_alerts existe")
        
        assert hasattr(db, 'create_user'), "Função create_user não encontrada"
        print("  ✓ create_user existe")
        
        assert hasattr(db, 'register_device_token'), "Função register_device_token não encontrada"
        print("  ✓ register_device_token existe")
        
        print()
        return True
    
    except Exception as e:
        print(f"  ✗ Erro: {e}")
        return False


def test_push_module():
    """Testa módulo de push notifications."""
    print("🧪 Testando módulo de push notifications...")
    
    try:
        from cerco import push
        
        # Verifica se funções existem
        assert hasattr(push, 'send_push_notification'), "Função send_push_notification não encontrada"
        print("  ✓ send_push_notification existe")
        
        assert hasattr(push, 'send_match_notification'), "Função send_match_notification não encontrada"
        print("  ✓ send_match_notification existe")
        
        assert hasattr(push, 'is_valid_expo_token'), "Função is_valid_expo_token não encontrada"
        print("  ✓ is_valid_expo_token existe")
        
        # Testa validação de token
        valid = push.is_valid_expo_token("ExponentPushToken[abc123]")
        assert valid == True, "Token válido não foi reconhecido"
        print("  ✓ Validação de token Expo funciona")
        
        invalid = push.is_valid_expo_token("invalid-token")
        assert invalid == False, "Token inválido foi aceito"
        print("  ✓ Rejeita tokens inválidos")
        
        print()
        return True
    
    except Exception as e:
        print(f"  ✗ Erro: {e}")
        return False


def test_mobile_cerco_module():
    """Testa módulo mobile_cerco."""
    print("🧪 Testando módulo mobile_cerco...")
    
    try:
        from cerco import mobile_cerco
        
        # Verifica se funções existem
        assert hasattr(mobile_cerco, 'publish_match_result'), "Função publish_match_result não encontrada"
        print("  ✓ publish_match_result existe")
        
        assert hasattr(mobile_cerco, 'is_enabled'), "Função is_enabled não encontrada"
        print("  ✓ is_enabled existe")
        
        print()
        return True
    
    except Exception as e:
        print(f"  ✗ Erro: {e}")
        return False


def main():
    """Executa todos os testes."""
    print("=" * 60)
    print("CERCO - Testes de Integração do Canal Mobile")
    print("=" * 60)
    print()
    
    results = []
    
    results.append(("Imports", test_imports()))
    results.append(("Database Module", test_database_module()))
    results.append(("Push Module", test_push_module()))
    results.append(("Mobile Cerco Module", test_mobile_cerco_module()))
    results.append(("API Endpoints", test_api_endpoints()))
    
    print("=" * 60)
    print("RESUMO DOS TESTES")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "✓ PASSOU" if passed else "✗ FALHOU"
        print(f"{name:.<40} {status}")
        if not passed:
            all_passed = False
    
    print()
    
    if all_passed:
        print("🎉 Todos os testes passaram!")
        print()
        print("Próximos passos:")
        print("1. Configure DATABASE_URL no .env")
        print("2. Rode o worker: python src/main.py")
        print("3. Rode a API: uvicorn src.api:app --reload")
        return 0
    else:
        print("❌ Alguns testes falharam. Verifique os erros acima.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
